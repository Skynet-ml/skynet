"""Verify data-free repository conversion and its owner boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import dspy
import pytest
from fastapi import HTTPException

from ...connectors.github_create_repo import CreatedRepository, RepoCreateError
from ..auth import AuthenticatedUser
from ..routers.optimizations import program_repository as migration


class Store:
    """Hold one synthetic run without network or database writes."""

    engine = None

    def __init__(self) -> None:
        """Build a completed program with private example data."""
        program = dspy.Predict("question -> answer")
        program.demos = [dspy.Example(question="private-demo", answer="private-answer")]
        self.job = {
            "optimization_id": "synthetic-id",
            "username": "owner",
            "status": "success",
            "optimization_type": "run",
            "payload_overview": {"username": "owner", "optimization_type": "run", "name": "Program"},
            "payload": {
                "module_name": "predict",
                "signature_code": "import dspy\nclass Answer(dspy.Signature):\n    question: str = dspy.InputField()\n    answer: str = dspy.OutputField()\n",
                "metric_code": "def metric(example, prediction): return example.answer == prediction.answer\n",
                "column_mapping": {"inputs": {"question": "q"}, "outputs": {"answer": "gold"}},
                "dataset": [{"q": "private-row", "gold": "private-output"}],
                "model_config": {"name": "openrouter/openai/gpt-4.1-mini"},
                "seed": 1,
            },
            "result": {
                "program_artifact": {"program_state_json": program.dump_state()},
                "baseline_test_metric": 0.2,
                "optimized_test_metric": 0.8,
                "runtime_seconds": 2,
                "optimized_test_results": [{"outputs": {"answer": "private-output"}, "score": 1}],
            },
        }
        self.writes = 0

    def get_job(self, optimization_id: str, **kwargs: Any) -> dict[str, Any]:
        """Read the synthetic job by id."""
        return deepcopy(self.job)

    def update_job(self, optimization_id: str, **fields: Any) -> None:
        """Persist one atomic replacement for assertions."""
        self.job.update(fields)
        self.writes += 1


def test_conversion_preserves_identity_and_scores_without_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publish only schema content and replay without creating another repository."""
    store = Store()
    publications = []

    def publish(token: str, name: str, files: dict[str, str]) -> CreatedRepository:
        """Record the prepared files without publishing externally."""
        publications.append(files)
        return CreatedRepository(f"owner/{name}", "main", "a" * 40)

    monkeypatch.setattr(migration, "_publish", publish)
    monkeypatch.setattr(migration, "github_token", lambda *_: "synthetic-token")
    monkeypatch.setattr(migration, "enforce_storage_quota", lambda *_, **__: None)
    user = AuthenticatedUser("owner", "user", ())
    first = migration.convert_program_repository(store, "synthetic-id", user)
    assert migration.convert_program_repository(store, "synthetic-id", user) == first
    assert len(publications) == store.writes == 1
    assert store.job["optimization_id"] == "synthetic-id"
    assert store.job["optimization_type"] == "blackbox"
    assert store.job["payload"]["cases"] is None
    assert store.job["result"]["baseline_score"] == store.job["result"]["best_score"] == 0.8
    assert store.job["result"]["details"]["program_origin"]["baseline_test_metric"] == 0.2
    assert store.job["payload_overview"]["serving_enabled"] is False
    assert store.job["result"]["program_artifact"] is None
    assert "private-row" not in repr(publications) + repr(store.job)
    assert "private-demo" not in repr(publications) + repr(store.job)
    assert "private-output" not in repr(publications) + repr(store.job)


def test_publication_failure_keeps_original_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep saved results untouched when GitHub refuses the initial publication."""
    store = Store()
    original = deepcopy(store.job)

    def fail(*args: Any) -> CreatedRepository:
        """Simulate a failed repository push."""
        raise RepoCreateError("Synthetic failure")

    monkeypatch.setattr(migration, "_publish", fail)
    monkeypatch.setattr(migration, "github_token", lambda *_: "synthetic-token")
    monkeypatch.setattr(migration, "enforce_storage_quota", lambda *_, **__: None)
    with pytest.raises(HTTPException, match="Synthetic failure"):
        migration.convert_program_repository(store, "synthetic-id", AuthenticatedUser("owner", "user", ()))
    assert store.job == original
    assert store.writes == 0


def test_shared_owner_cannot_publish_another_owners_program(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require the original owner even when a share grant permits reading."""
    store = Store()
    monkeypatch.setattr(migration, "load_job_for_user", lambda *_: store.job)
    with pytest.raises(HTTPException) as error:
        migration.convert_program_repository(store, "synthetic-id", AuthenticatedUser("other", "user", ()))
    assert error.value.status_code == 403
    assert store.writes == 0

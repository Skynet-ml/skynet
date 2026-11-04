"""Tests for ``POST /optimize``: staged datasets, task seeding, contract errors and the stored overview."""

from __future__ import annotations

from typing import Any

import pytest

from ...constants import OPTIMIZATION_TYPE_BLACKBOX
from ...models import UnifiedRunRequest
from ...models.blackbox import BLACKBOX_MODULE_NAME
from ...service_gateway.optimization.unified.split import task_fingerprint
from ..routers import submissions as _sub_mod
from ..routers._helpers import stable_seed
from .test_submissions_router import _FakeJobStore, make_client, unified_payload


def _submit(client: Any, body: dict) -> tuple[int, dict]:
    """Post one unified run.

    Args:
        client: Test client over the submissions router.
        body: Request body.

    Returns:
        The status code and the JSON body.
    """
    resp = client.post("/optimize", json=body)
    return resp.status_code, resp.json()


def _stored(store: _FakeJobStore, optimization_id: str) -> tuple[dict, dict]:
    """Return the persisted payload and overview of one job.

    Args:
        store: Fake job store.
        optimization_id: Job id.

    Returns:
        The payload and overview dicts.
    """
    row = store._jobs[optimization_id]
    return row["payload"], row["overview"]


def test_happy_path_stores_a_unified_blackbox_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A submit stores a black-box run whose payload carries the unified spec."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    status, body = _submit(client, unified_payload())

    assert status == 201, body
    payload, overview = _stored(store, body["optimization_id"])
    assert payload["payload_kind"] == "unified_v1"
    assert payload["workspace"]["metric_code"].startswith("def score")
    assert len(payload["data"]["cases"]) == 6
    assert overview["optimization_type"] == OPTIMIZATION_TYPE_BLACKBOX
    assert overview["module_name"] == BLACKBOX_MODULE_NAME
    assert body["module_name"] == BLACKBOX_MODULE_NAME


def test_overview_carries_payload_kind_and_split_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The overview has the wizard's keys and train/val/test counts that cover every case."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    status, body = _submit(client, unified_payload(optimizer={"choice": "auto"}))

    assert status == 201, body
    _payload, overview = _stored(store, body["optimization_id"])
    assert overview["payload_kind"] == "unified_v1"
    assert overview["artifact_kind"] == "dspy_program"
    assert overview["optimizer_choice"] == "auto"
    assert overview["optimizer_resolved"] == "dspy_gepa"
    assert overview["optimizer_name"] == "dspy_gepa"
    assert overview["data_mode"] == "pairs"
    assert overview["dataset_rows"] == 6
    counts = overview["split_counts"]
    assert set(counts) == {"train", "val", "test"}
    assert counts["train"] + counts["val"] + counts["test"] == 6
    assert counts["test"] >= 1


def test_overview_keeps_the_fields_the_run_views_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """List and detail views read the model, owner and billing fields from the overview."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    body = unified_payload(
        models={
            "task_model": {"name": "openai/gpt-4o-mini"},
            "reflection_model": {"name": "openai/gpt-4o"},
        },
        is_private=True,
    )

    status, resp = _submit(client, body)

    assert status == 201, resp
    _payload, overview = _stored(store, resp["optimization_id"])
    assert overview["username"] == "alice"
    assert overview["name"] == "unified-run"
    assert overview["model_name"] == overview["reflection_model_name"]
    assert "gpt-4o" in overview["model_name"]
    assert "gpt-4o-mini" in overview["task_model_name"]
    assert overview["is_private"] is True
    assert overview["token_source"] == "managed"


def test_dspy_program_auto_resolves_to_dspy_gepa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto on a DSPy program is stored as DSPy GEPA."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    body = unified_payload(optimizer={"choice": "auto"})
    body["task"]["artifact"] = {"kind": "dspy_program", "starting_version": {"predict": "Answer."}}

    status, resp = _submit(client, body)

    assert status == 201, resp
    _payload, overview = _stored(store, resp["optimization_id"])
    assert overview["optimizer_resolved"] == "dspy_gepa"
    assert resp["optimizer_name"] == "dspy_gepa"


def test_seed_comes_from_the_task_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no seed posted, the split seed is derived from the task, not the run id."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    body = unified_payload()

    status, resp = _submit(client, body)

    assert status == 201, resp
    payload, overview = _stored(store, resp["optimization_id"])
    expected = stable_seed(task_fingerprint(UnifiedRunRequest.model_validate(body)))
    assert payload["seed"] == expected
    assert overview["seed"] == expected


def test_resubmitting_the_same_task_reuses_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two separate submits of one task get different ids but the same seed and split."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    # A capped ceiling leaves balance for the second run; an uncapped one commits it all.
    _, first = _submit(client, unified_payload(name="first", max_cost_cents=100))
    _, second = _submit(client, unified_payload(name="second", max_cost_cents=100))

    assert first["optimization_id"] != second["optimization_id"]
    first_payload, first_overview = _stored(store, first["optimization_id"])
    second_payload, second_overview = _stored(store, second["optimization_id"])
    assert first_payload["seed"] == second_payload["seed"]
    assert first_overview["split_counts"] == second_overview["split_counts"]


def test_posted_seed_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit seed wins over the fingerprint."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    _, resp = _submit(client, unified_payload(seed=7))

    payload, _overview = _stored(store, resp["optimization_id"])
    assert payload["seed"] == 7


def test_optimizer_outside_the_setup_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """A black-box engine on a DSPy program is rejected by request validation."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    status, body = _submit(client, unified_payload(optimizer={"choice": "gepa"}))

    assert status == 422
    assert "gepa" in str(body) or "optimizer" in str(body).lower()
    assert store.created_ids() == []


def test_contract_violation_is_422_with_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace that does not fit the optimizer is refused with the contract's message."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    def reject(_payload: UnifiedRunRequest) -> None:
        """Fail the static contract check.

        Raises:
            ValueError: Always.
        """
        raise ValueError("metric must define score(input, output)")

    monkeypatch.setattr(_sub_mod, "validate_unified_payload", reject)

    status, body = _submit(client, unified_payload())

    assert status == 422
    assert body["code"] == "submit.unified.contract_failed"
    assert "score(input, output)" in str(body["params"])
    assert store.created_ids() == []


def test_staged_dataset_becomes_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Staged rows become cases shaped like the wizard's inline ones: expected keyed by the expected field."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    rows = [{"question": f"Q{i}?", "context": f"C{i}", "answer": f"A{i}", "note": "x"} for i in range(5)]
    staged_id = store.stage_dataset("alice", "pairs.csv", rows)
    body = unified_payload()
    body["data"] = {
        "mode": "pairs",
        "staged_dataset_id": staged_id,
        "input_fields": ["question", "context"],
        "expected_field": "answer",
    }

    status, resp = _submit(client, body)

    assert status == 201, resp
    payload, overview = _stored(store, resp["optimization_id"])
    cases = payload["data"]["cases"]
    assert payload["data"]["staged_dataset_id"] is None
    assert cases[0] == {"input": {"question": "Q0?", "context": "C0"}, "expected": {"answer": "A0"}}
    assert len(cases) == 5
    assert overview["dataset_rows"] == 5


def test_staged_dataset_without_expected_field_keeps_every_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no input fields and no expected field, every column is input."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    staged_id = store.stage_dataset("alice", "one.csv", [{"text": "hello", "lang": "en"}])
    body = unified_payload()
    body["data"] = {"mode": "single", "staged_dataset_id": staged_id}

    status, resp = _submit(client, body)

    assert status == 201, resp
    payload, overview = _stored(store, resp["optimization_id"])
    assert payload["data"]["cases"] == [{"input": {"text": "hello", "lang": "en"}, "expected": None}]
    assert overview["split_counts"] == {"train": 1, "val": 1, "test": 1}


def test_unknown_staged_dataset_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A staged id the caller does not own is refused before anything is stored."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    other = store.stage_dataset("bob", "x.csv", [{"q": "1"}, {"q": "2"}])
    body = unified_payload()
    body["data"] = {"mode": "pairs", "staged_dataset_id": other}

    status, resp = _submit(client, body)

    assert status == 400
    assert resp["code"] == "submission.staged_dataset_not_found"
    assert store.created_ids() == []


def test_staged_rows_that_do_not_fit_the_mode_are_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-input run cannot be fed a multi-row table."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)
    staged_id = store.stage_dataset("alice", "many.csv", [{"q": "1"}, {"q": "2"}])
    body = unified_payload()
    body["data"] = {"mode": "single", "staged_dataset_id": staged_id}

    status, resp = _submit(client, body)

    assert status == 422
    assert resp["code"] == "submit.unified.contract_failed"
    assert store.get_staged_dataset(staged_id, "alice") is not None

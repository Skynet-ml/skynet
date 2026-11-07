"""Tests for the run record store: secret strip, body cut, call order, cursor and candidate totals."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from core.storage.models import Base
from core.storage.run_call_store import BODY_CAP_BYTES, SECRET_MASK, PostgresRunCallStore, cut_body, strip_secrets

SECRET = 'sk-test-"abc\\def'


@pytest.fixture
def store() -> PostgresRunCallStore:
    """Return a store over a fresh in-memory SQLite schema."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return PostgresRunCallStore(engine)


def _call(**overrides: object) -> dict[str, object]:
    """Build one relay call as the relay sends it.

    Args:
        **overrides: Field overrides.

    Returns:
        The call.
    """
    call: dict[str, object] = {
        "candidate_id": "cand-1",
        "role": "solver",
        "kind": "model",
        "target": "openai/gpt-5",
        "started_at": "2026-11-01T10:00:00+00:00",
        "duration_ms": 120,
        "status": "ok",
        "failure_reason": None,
        "cost_usd": 0.25,
        "request": '{"prompt": "hi"}',
        "response": '{"text": "hello"}',
    }
    call.update(overrides)
    return call


def test_strip_secrets_masks_the_raw_and_the_json_escaped_form() -> None:
    """A secret is masked both as written and as a JSON encoder escapes it."""
    escaped = json.dumps(SECRET)[1:-1]
    text = f"raw={SECRET} echoed={escaped}"

    cleaned = strip_secrets(text, [SECRET, ""])

    assert SECRET not in cleaned
    assert escaped not in cleaned
    assert cleaned == f"raw={SECRET_MASK} echoed={SECRET_MASK}"


def test_strip_secrets_masks_a_longer_secret_whole() -> None:
    """A secret that contains a shorter secret leaves no remainder behind."""
    assert strip_secrets("key=abcdef", ["abc", "abcdef"]) == f"key={SECRET_MASK}"


def test_cut_body_keeps_small_bodies_and_reports_the_full_size_of_large_ones() -> None:
    """Small bodies pass whole; large bodies are cut to the cap and keep their full size."""
    assert cut_body("hello", cap=10) == ("hello", 5, False)

    text, size, truncated = cut_body("é" * 10, cap=5)

    assert truncated is True
    assert size == 20
    assert text == "éé"


def test_record_call_cuts_a_large_body_and_keeps_its_full_size(store: PostgresRunCallStore) -> None:
    """A response past the cap is stored cut, with the full byte count and the cut flag."""
    big = "x" * (BODY_CAP_BYTES + 100)
    store.record_call("job-1", _call(response=big), [])

    [record] = store.list_calls("job-1", "cand-1")

    assert len(record["response"].encode()) == BODY_CAP_BYTES
    assert record["response_bytes"] == BODY_CAP_BYTES + 100
    assert record["response_truncated"] is True
    assert record["request_truncated"] is False


def test_record_call_strips_secrets_from_bodies_target_and_failure(store: PostgresRunCallStore) -> None:
    """A secret sent in the request and echoed back escaped in the response is stored nowhere."""
    escaped = json.dumps(SECRET)[1:-1]
    store.record_call(
        "job-1",
        _call(
            target=f"https://api.example.test/?key={SECRET}",
            request=f'{{"authorization": "Bearer {SECRET}"}}',
            response=f'{{"echo": "{escaped}"}}',
            status="error",
            failure_reason=f"bad key {SECRET}",
        ),
        [SECRET],
    )

    [record] = store.list_calls("job-1", "cand-1")

    for field in ("target", "request", "response", "failure_reason"):
        assert SECRET not in record[field]
        assert escaped not in record[field]
        assert SECRET_MASK in record[field]


def test_record_call_strips_a_secret_that_spans_the_cut(store: PostgresRunCallStore) -> None:
    """A secret across the cap boundary is masked before the cut, so no part of it is kept."""
    secret = "s3cr3t-value-0123456789"
    body = "x" * (BODY_CAP_BYTES - 5) + secret + "tail"
    store.record_call("job-1", _call(request=body), [secret])

    [record] = store.list_calls("job-1", "cand-1")

    assert secret[:5] not in record["request"][-10:]
    assert record["request_bytes"] == len(body) - len(secret) + len(SECRET_MASK)


def test_record_call_rejects_an_unknown_kind(store: PostgresRunCallStore) -> None:
    """Only the known call kinds are stored."""
    with pytest.raises(ValueError):
        store.record_call("job-1", _call(kind="shell"), [])


def test_list_calls_returns_calls_in_write_order_after_the_cursor(store: PostgresRunCallStore) -> None:
    """Calls come back oldest first; ``since`` skips held calls and ``limit`` caps the page."""
    ids = [store.record_call("job-1", _call(target=f"t{i}"), []) for i in range(5)]
    store.record_call("job-1", _call(candidate_id="cand-2"), [])
    store.record_call("job-2", _call(), [])

    assert [c["target"] for c in store.list_calls("job-1", "cand-1")] == [f"t{i}" for i in range(5)]
    assert [c["id"] for c in store.list_calls("job-1", "cand-1", since=ids[1])] == ids[2:]
    assert [c["id"] for c in store.list_calls("job-1", "cand-1", since=ids[0], limit=2)] == ids[1:3]
    assert store.list_calls("job-1", "cand-1", since=ids[-1]) == []


def test_list_candidates_reports_role_state_count_and_cost(store: PostgresRunCallStore) -> None:
    """Each candidate shows its role, state, call count and summed cost; a new one starts running."""
    store.record_call("job-1", _call(cost_usd=0.25), [])
    store.record_call("job-1", _call(cost_usd=0.5), [])
    store.record_call("job-1", _call(cost_usd=None), [])
    store.record_call("job-1", _call(candidate_id="cand-2", role="judge", kind="evaluator", cost_usd=None), [])
    store.set_candidate_state("job-1", "cand-1", "solver", "done")

    candidates = {c["candidate_id"]: c for c in store.list_candidates("job-1")}

    assert candidates["cand-1"]["state"] == "done"
    assert candidates["cand-1"]["call_count"] == 3
    assert candidates["cand-1"]["cost_usd"] == pytest.approx(0.75)
    assert candidates["cand-2"] == {
        **candidates["cand-2"],
        "role": "judge",
        "state": "running",
        "call_count": 1,
        "cost_usd": 0.0,
    }
    assert store.get_candidate("job-1", "cand-2")["role"] == "judge"
    assert store.get_candidate("job-1", "missing") is None
    assert store.list_candidates("job-2") == []


def test_set_candidate_state_creates_a_candidate_and_rejects_an_unknown_state(store: PostgresRunCallStore) -> None:
    """A state mark creates a candidate with no calls; an unknown state is refused."""
    store.set_candidate_state("job-1", "cand-9", "solver", "failed")

    assert store.get_candidate("job-1", "cand-9")["state"] == "failed"
    assert store.get_candidate("job-1", "cand-9")["call_count"] == 0
    with pytest.raises(ValueError):
        store.set_candidate_state("job-1", "cand-9", "solver", "paused")

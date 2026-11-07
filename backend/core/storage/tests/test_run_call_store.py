"""Tests for the run record store: secret strip, body cut, call order, cursor and candidate totals.

The tests at the end need row locks, which SQLite lacks. They run only when
``APP_TEST_DB_URL`` names a disposable PostgreSQL database, and each one works
in its own schema that it drops after.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from core.config import app_env
from core.storage.models import Base, JobModel, RunCallModel, RunCandidateModel
from core.storage.run_call_store import BODY_CAP_BYTES, SECRET_MASK, PostgresRunCallStore, cut_body, strip_secrets

SECRET = 'sk-test-"abc\\def'
TEST_DB_URL = app_env("APP_TEST_DB_URL")
needs_postgres = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_TEST_DB_URL to a disposable PostgreSQL database.",
)


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
    seqs = [store.record_call("job-1", _call(target=f"t{i}"), []) for i in range(5)]
    other = store.record_call("job-1", _call(candidate_id="cand-2"), [])
    store.record_call("job-2", _call(), [])

    assert seqs == [1, 2, 3, 4, 5]
    assert other == 1
    assert [c["target"] for c in store.list_calls("job-1", "cand-1")] == [f"t{i}" for i in range(5)]
    assert [c["seq"] for c in store.list_calls("job-1", "cand-1", since=seqs[1])] == seqs[2:]
    assert [c["seq"] for c in store.list_calls("job-1", "cand-1", since=seqs[0], limit=2)] == seqs[1:3]
    assert store.list_calls("job-1", "cand-1", since=seqs[-1]) == []


def test_a_long_candidate_id_reads_back_by_the_same_cut(store: PostgresRunCallStore) -> None:
    """Writes keep 64 characters of a candidate id, and reads apply the same cut."""
    long_id = "c" * 80
    store.record_call("job-1", _call(candidate_id=long_id), [])

    assert [c["candidate_id"] for c in store.list_calls("job-1", long_id)] == ["c" * 64]
    assert store.get_candidate("job-1", long_id)["call_count"] == 1


def test_list_candidates_pages_with_limit_and_offset(store: PostgresRunCallStore) -> None:
    """Candidates come in start order, one page at a time, each with only its own totals."""
    for index in range(5):
        store.record_call("job-1", _call(candidate_id=f"cand-{index}", cost_usd=float(index)), [])

    first = store.list_candidates("job-1", limit=2)
    rest = store.list_candidates("job-1", limit=10, offset=2)

    assert [c["candidate_id"] for c in first] == ["cand-0", "cand-1"]
    assert [c["candidate_id"] for c in rest] == ["cand-2", "cand-3", "cand-4"]
    assert [c["cost_usd"] for c in first + rest] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert store.list_candidates("job-1", offset=5) == []


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


@pytest.fixture
def postgres() -> Iterator[Engine]:
    """Return an engine on a fresh schema of the test database, with one job row."""
    schema = f"run_calls_test_{uuid.uuid4().hex}"
    admin = create_engine(TEST_DB_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(TEST_DB_URL, connect_args={"options": f"-csearch_path={schema}"}, pool_size=20)
    try:
        Base.metadata.create_all(
            engine, tables=[JobModel.__table__, RunCallModel.__table__, RunCandidateModel.__table__]
        )
        with Session(engine) as session:
            session.add(JobModel(optimization_id="job-1"))
            session.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@needs_postgres
def test_a_write_waits_while_another_write_of_the_candidate_holds_its_row(postgres: Engine) -> None:
    """A second call of a candidate cannot commit before the first: it waits on the candidate row lock."""
    store = PostgresRunCallStore(postgres)
    store.record_call("job-1", _call(), [])
    with ThreadPoolExecutor(max_workers=1) as pool, Session(postgres) as holder:
        holder.execute(
            text("SELECT 1 FROM run_candidates WHERE optimization_id = 'job-1' AND candidate_id = 'cand-1' FOR UPDATE")
        )
        waiting = pool.submit(store.record_call, "job-1", _call(target="late"), [])

        time.sleep(0.5)
        assert not waiting.done()
        assert [c["seq"] for c in store.list_calls("job-1", "cand-1")] == [1]
        holder.commit()

        assert waiting.result(timeout=10) == 2
    assert [c["target"] for c in store.list_calls("job-1", "cand-1", since=1)] == ["late"]


@needs_postgres
def test_a_poll_during_parallel_writes_never_skips_a_call(postgres: Engine) -> None:
    """A monitor that polls with ``next_since`` while calls commit in parallel sees every call once."""
    store = PostgresRunCallStore(postgres)
    writes = 40
    seen: list[int] = []
    finished = threading.Event()

    def poll() -> None:
        """Poll the candidate the way the live route does, until the writers end and nothing is new."""
        since = 0
        while True:
            done = finished.is_set()
            page = store.list_calls("job-1", "cand-1", since=since, limit=500)
            seen.extend(call["seq"] for call in page)
            since = page[-1]["seq"] if page else since
            if done and not page:
                return

    poller = threading.Thread(target=poll)
    poller.start()
    with ThreadPoolExecutor(max_workers=8) as pool:
        seqs = list(pool.map(lambda index: store.record_call("job-1", _call(target=f"t{index}"), []), range(writes)))
    finished.set()
    poller.join(timeout=30)

    assert sorted(seqs) == list(range(1, writes + 1))
    assert seen == list(range(1, writes + 1))
    assert store.get_candidate("job-1", "cand-1")["call_count"] == writes

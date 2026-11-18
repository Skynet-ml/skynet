"""Live-Postgres concurrency tests for ``RemoteDBJobStore.claim_next_job`` and the sandbox trial rows.

These tests verify that ``FOR UPDATE SKIP LOCKED`` actually serializes
concurrent claimers — something SQLite cannot exercise because it lacks
row-level locking. They are gated on ``APP_TEST_DB_URL`` — a dedicated
opt-in, **never** ``REMOTE_DB_URL``, because setup can drop a legacy schema
and the tests mutate job rows, while ``tests/conftest.py`` loads
``backend/.env`` into the environment: a developer's ordinary config must
not point these tests at their local database. Use a throwaway::

    APP_TEST_DB_URL=postgresql://skynet:skynet@localhost:5432/testdb \
        pytest backend/core/storage/tests/test_remote_jobstore_postgres.py

Without that env var the entire module is skipped, so unit-test CI is
unaffected.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pickle
import random
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from gepa.core.state import GEPAState, ValsetEvaluation
from pydantic import SecretStr
from sqlalchemy import create_engine, event, make_url, text
from sqlalchemy.orm import Session

import core.storage.remote as remote_mod
from alembic import command
from core.billing.budgets import BudgetService
from core.config import app_env, settings
from core.constants import PROGRESS_BASELINE
from core.sandbox import ResourceRequest, SandboxEnvironment, SandboxSpec
from core.sandbox.pool import BoxOutcome, BoxWork, DurableTrials, SandboxPool, TrialFingerprintError, TrialStatus
from core.sandbox.tests.fake_provider import InMemorySandbox
from core.storage.models import MAX_PROGRESS_EVENT_KEY_LENGTH, PROGRESS_EVENT_KEY, BillingCustomerModel
from core.storage.remote import RemoteDBJobStore
from core.worker.checkpoint_compat import checkpoint_manifest

TEST_DB_URL = app_env("APP_TEST_DB_URL")
BACKEND_DIR = Path(__file__).resolve().parents[3]
# Shared by the job store and notification test modules, so their xdist workers migrate APP_TEST_DB_URL one at a time.
_MIGRATE_LOCK = Path(tempfile.gettempdir()) / "skynet-postgres-tests-migrate.lock"
# xdist runs the tests of this module on several workers at once, and claim_next_job claims any pending job.
_JOBS_TEST_LOCK = Path(tempfile.gettempdir()) / "skynet-postgres-tests-jobs.lock"

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="APP_TEST_DB_URL not set to a postgresql:// URL — skipping live-DB tests.",
)


def _reset_legacy_schema(db_url: str) -> None:
    """Drop ``public`` if the target DB holds pre-Alembic legacy tables.

    The baseline migration uses ``CREATE TABLE IF NOT EXISTS`` for idempotency
    on properly-versioned databases, but a target that was bootstrapped by
    hand for an earlier release leaves stale, schema-incompatible tables
    (e.g. an old ``job_logs`` keyed on ``job_id`` instead of
    ``optimization_id``) that the guard skips over. Detect that legacy state
    by the absence of ``alembic_version`` and a non-empty ``public`` schema,
    and reset it so the upgrade runs against a clean slate. Properly-migrated
    and empty databases are left untouched.

    Args:
        db_url: SQLAlchemy URL of the live Postgres test target.
    """
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            already_versioned = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'alembic_version'"
                )
            ).scalar()
            if already_versioned:
                return
            has_legacy_tables = conn.execute(
                text("SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' LIMIT 1")
            ).scalar()
            if not has_legacy_tables:
                return
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.commit()
    finally:
        engine.dispose()


def _upgrade_to_head(db_url: str) -> None:
    """Upgrade ``db_url`` to Alembic head, and never touch the ``REMOTE_DB_URL`` database.

    ``alembic/env.py`` replaces ``sqlalchemy.url`` with ``REMOTE_DB_URL`` when
    that variable is set, and ``tests/conftest.py`` loads a developer's
    ``backend/.env`` (which points it at the dev database). So the variable
    is set to the test URL for the length of the upgrade only (SKY-803).
    A call from code has no CLI options, so ``env.py`` also upgrades every
    ``STEP_LOG_SHARDS`` database that ``backend/.env`` names. The shard map is
    empty for the length of the upgrade (SKY-824).

    Args:
        db_url: SQLAlchemy URL of the live Postgres test target.
    """
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REMOTE_DB_URL", db_url)
        mp.setattr(settings, "step_log_shards", {})
        command.upgrade(alembic_cfg, "head")


def migrate_test_db(db_url: str) -> None:
    """Reset a legacy schema in ``db_url`` and upgrade it to Alembic head, one xdist worker at a time.

    Args:
        db_url: SQLAlchemy URL of the live Postgres test target.
    """
    # Each xdist worker migrates the same empty database from its own module fixture, and concurrent
    # CREATE TABLE races in pg_type. A Postgres advisory lock would deadlock with CREATE INDEX CONCURRENTLY,
    # which waits for the lock holder's open transaction, so a file lock lets one worker migrate at a time.
    with _MIGRATE_LOCK.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        _reset_legacy_schema(db_url)
        _upgrade_to_head(db_url)


@pytest.fixture(scope="module")
def store() -> Iterator[RemoteDBJobStore]:
    """Yield a single store backed by the real Postgres for the whole module.

    The live database is upgraded to Alembic head first so model changes
    such as new indexed columns are present before rows are inserted.
    The store is reused across tests to avoid repeating that setup work.
    """
    migrate_test_db(TEST_DB_URL or "")

    s = RemoteDBJobStore(TEST_DB_URL)
    try:
        yield s
    finally:
        s.engine.dispose()


@pytest.fixture(autouse=True)
def _one_jobs_test_at_a_time() -> Iterator[None]:
    """Run the tests of this module one at a time across xdist workers.

    A claim in one test can take a job that another test seeded, and a resume
    sets a claimed job back to pending, so the concurrent claimers saw foreign
    and duplicate claims about 1 run in 3 (SKY-824).

    Yields:
        Nothing. The lock is held for the length of the test and its fixtures.
    """
    with _JOBS_TEST_LOCK.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


def test_alembic_setup_uses_the_test_url_when_remote_db_url_points_elsewhere(
    store: RemoteDBJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade the ``APP_TEST_DB_URL`` database even when ``REMOTE_DB_URL`` names another database.

    The ``store`` fixture runs first, so a legacy test database gets its reset before this upgrade.
    """
    other_url = make_url(TEST_DB_URL or "").set(database=f"sky803_missing_{uuid.uuid4().hex[:8]}")
    other = other_url.render_as_string(hide_password=False)
    monkeypatch.setenv("REMOTE_DB_URL", other)

    # The other database does not exist, so an upgrade that went to it would raise.
    _upgrade_to_head(TEST_DB_URL or "")

    engine = create_engine(TEST_DB_URL or "")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()
    assert os.environ["REMOTE_DB_URL"] == other


def test_alembic_setup_skips_the_step_log_shards_from_the_settings(
    store: RemoteDBJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade only the test database, even when ``STEP_LOG_SHARDS`` names another database.

    Args:
        store: Used only so that its fixture migrates the test database first.
        monkeypatch: Sets ``settings.step_log_shards`` for this test.
    """
    shard_url = make_url(TEST_DB_URL or "").set(database=f"sky824_missing_shard_{uuid.uuid4().hex[:8]}")
    shards = {"proof": SecretStr(shard_url.render_as_string(hide_password=False))}
    monkeypatch.setattr(settings, "step_log_shards", shards)

    # The shard database does not exist, so a shard upgrade would raise.
    _upgrade_to_head(TEST_DB_URL or "")

    assert settings.step_log_shards is shards


@pytest.fixture
def seeded_jobs(store: RemoteDBJobStore) -> Iterator[list[str]]:
    """Seed a uniquely-prefixed batch of pending jobs and clean them up after.

    Using a per-test UUID prefix keeps these tests safe to run against a
    shared Postgres without colliding with real data or with parallel
    test runs.
    """
    prefix = f"concurrency-test-{uuid.uuid4().hex[:8]}"
    ids = [f"{prefix}-{i:03d}" for i in range(20)]
    for oid in ids:
        store.create_job(oid)
        # A pending row is only claimable once its payload is written (see
        # claim_next_job's payload-presence guard), so seed one here.
        store.update_job(oid, payload={"ok": True})
    try:
        yield ids
    finally:
        store.delete_jobs(ids)


def test_concurrent_claimers_never_collide(store: RemoteDBJobStore, seeded_jobs: list[str]) -> None:
    """Twenty pending rows + ten concurrent claimers → every claim is unique.

    Each worker drains aggressively (stops only after several consecutive
    empty responses) because ``FOR UPDATE SKIP LOCKED`` can transiently
    return ``None`` while a peer transaction holds the row lock during
    commit — a single ``None`` does not mean the queue is empty. We assert
    (a) no two workers received the same ``optimization_id`` and (b) every
    seeded job was claimed exactly once.
    """
    expected_ids = set(seeded_jobs)
    worker_count = 10
    empty_streak_to_stop = 5

    def worker(worker_idx: int) -> list[str]:
        """Drain the pending queue from this worker until the streak threshold."""
        worker_id = f"pod-{worker_idx}"
        out: list[str] = []
        empty_streak = 0
        while empty_streak < empty_streak_to_stop:
            row = store.claim_next_job(worker_id, lease_seconds=60.0)
            if row is None:
                empty_streak += 1
                time.sleep(0.01)
                continue
            empty_streak = 0
            out.append(row["optimization_id"])
        return out

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = list(pool.map(worker, range(worker_count)))

    claimed: list[str] = [oid for batch in results for oid in batch]

    assert len(claimed) == len(set(claimed)), (
        f"Duplicate claim detected — FOR UPDATE SKIP LOCKED failed. "
        f"Got {len(claimed)} claims but only {len(set(claimed))} unique IDs."
    )
    assert set(claimed).issubset(expected_ids), "Claim returned an unexpected ID — test bled into other rows."
    assert len(claimed) == len(expected_ids), (
        f"Expected all {len(expected_ids)} seeded jobs to be claimed, got {len(claimed)}."
    )


def test_claim_records_owner_and_lease(store: RemoteDBJobStore, seeded_jobs: list[str]) -> None:
    """A successful claim must persist ``claimed_by`` and ``lease_expires_at``.

    This is the read-back path the orphan-recovery loop relies on; if
    the UPDATE silently dropped these columns the lease would never
    expire and stuck workers would never be reclaimed. The columns
    aren't in the public TypedDict, so we query them directly.
    """
    row = store.claim_next_job("pod-solo", lease_seconds=42.0)
    assert row is not None
    assert row["status"] == "validating"

    with store.engine.connect() as conn:
        result = conn.execute(
            text("SELECT claimed_by, claimed_at, lease_expires_at FROM jobs WHERE optimization_id = :oid"),
            {"oid": row["optimization_id"]},
        ).fetchone()

    assert result is not None
    claimed_by, claimed_at, lease_expires_at = result
    assert claimed_by == "pod-solo"
    assert claimed_at is not None
    assert lease_expires_at is not None
    assert lease_expires_at > claimed_at


def test_parallel_progress_writers_finish_quickly(store: RemoteDBJobStore) -> None:
    """Parallel progress writers for distinct jobs avoid parent-row lock serialization."""
    prefix = f"progress-test-{uuid.uuid4().hex[:8]}"
    ids = [f"{prefix}-a", f"{prefix}-b"]
    for oid in ids:
        store.create_job(oid)

    def write_events(optimization_id: str) -> None:
        """Write a fixed batch of progress events for a job."""
        for i in range(100):
            store.record_progress(optimization_id, "optimizer_progress", {"i": i})

    try:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write_events, ids))
        elapsed = time.perf_counter() - started

        assert elapsed < 2.0
        assert store.get_progress_count(ids[0]) == 100
        assert store.get_progress_count(ids[1]) == 100
    finally:
        store.delete_jobs(ids)


def test_replayed_keyed_progress_events_add_one_row_each(
    store: RemoteDBJobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A replay adds no rows for repeated keys and keeps the order of the others (SKY-808).

    ``record_progress`` logs an insert error and goes on, so the test checks
    the insert result and the log: a unique-index error must not stand in for the skip.
    """
    optimization_id = f"progress-key-{uuid.uuid4().hex[:8]}"
    store.create_job(optimization_id)
    key = PROGRESS_EVENT_KEY
    now = datetime.now(UTC)
    try:
        inserted = [
            store._insert_progress_event(optimization_id, "candidate", {key: f"candidate:{index}"}, now)
            for index in (0, 1, 0, 1, 2)
        ]
        assert inserted == [True, True, False, False, True]
        with caplog.at_level(logging.WARNING, logger=remote_mod.__name__):
            store.record_progress(optimization_id, "candidate", {key: "candidate:1"})
            store.record_progress(optimization_id, "tick", {})
            store.record_progress(optimization_id, "tick", {})
        assert "Failed to insert progress event" not in caplog.text

        events = store.get_progress_events(optimization_id)
        assert [e["metrics"].get(key) for e in events] == ["candidate:0", "candidate:1", "candidate:2", None, None]
    finally:
        store.delete_jobs([optimization_id])


def test_record_progress_for_generation_replayed_keyed_event_adds_no_row(store: RemoteDBJobStore) -> None:
    """The generation-fenced write also adds one row per key and still refreshes the metrics."""
    optimization_id = f"progress-key-gen-{uuid.uuid4().hex[:8]}"
    store.create_job(optimization_id)
    store.update_job(optimization_id, status="running", execution_generation=1)
    try:
        for score in (0.5, 0.7):
            assert store.record_progress_for_generation(
                optimization_id,
                "step",
                {PROGRESS_EVENT_KEY: "step:3", "score": score},
                source_optimization_id=optimization_id,
                generation=1,
            )
        assert store.get_progress_count(optimization_id) == 1
        assert store.get_job(optimization_id)["latest_metrics"]["score"] == 0.7
    finally:
        store.delete_jobs([optimization_id])


def test_over_long_progress_event_key_counts_as_no_key(store: RemoteDBJobStore) -> None:
    """An over-long key adds a row each time; the index predicate and the code agree on the limit."""
    optimization_id = f"progress-key-long-{uuid.uuid4().hex[:8]}"
    store.create_job(optimization_id)
    metrics = {PROGRESS_EVENT_KEY: "k" * (MAX_PROGRESS_EVENT_KEY_LENGTH + 1)}
    try:
        store.record_progress(optimization_id, "step", metrics)
        store.record_progress(optimization_id, "step", metrics)
        assert store.get_progress_count(optimization_id) == 2
    finally:
        store.delete_jobs([optimization_id])


def test_concurrent_copies_of_a_keyed_progress_event_add_one_row(store: RemoteDBJobStore) -> None:
    """Writers that reach the insert at the same moment still add one row."""
    optimization_id = f"progress-key-race-{uuid.uuid4().hex[:8]}"
    store.create_job(optimization_id)
    metrics = {PROGRESS_EVENT_KEY: "step:7", "score": 0.5}
    writers = 4
    barrier = threading.Barrier(writers)

    def hold_progress_insert(*args: object) -> None:
        """Hold each progress insert until every writer reaches its own insert.

        Args:
            *args: The ``before_cursor_execute`` arguments; the third one is the SQL text.
        """
        statement = str(args[2])
        if statement.lstrip().upper().startswith("INSERT INTO JOB_PROGRESS_EVENTS"):
            # A writer that waits on a lock before its insert breaks the barrier; the others go on.
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=2)

    event.listen(store.engine, "before_cursor_execute", hold_progress_insert)
    try:
        with ThreadPoolExecutor(max_workers=writers) as pool:
            inserted = list(
                pool.map(
                    lambda _: store._insert_progress_event(optimization_id, "step", metrics, datetime.now(UTC)),
                    range(writers),
                )
            )
        assert sorted(inserted) == [False] * (writers - 1) + [True]
        assert store.get_progress_count(optimization_id) == 1
    finally:
        event.remove(store.engine, "before_cursor_execute", hold_progress_insert)
        store.delete_jobs([optimization_id])


def test_progress_event_key_index_exists_after_migration(store: RemoteDBJobStore) -> None:
    """The migration builds the unique partial index that the keyed insert targets."""
    with store.engine.connect() as conn:
        indexdef = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_job_progress_events_event_key'")
        ).scalar()
    assert indexdef is not None
    assert indexdef.startswith("CREATE UNIQUE INDEX")
    assert "jsonb_typeof" in indexdef


def test_progress_trim_keeps_structural_events_while_keyed_events_cover_the_excess(
    store: RemoteDBJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kept keyed non-structural rows cover the excess, so the trim does not delete the baseline (SKY-808)."""
    monkeypatch.setattr(store, "_max_progress_events", 3)
    monkeypatch.setattr(remote_mod, "PROGRESS_TRIM_SAMPLE_RATE", 1)
    optimization_id = f"progress-trim-{uuid.uuid4().hex[:8]}"
    store.create_job(optimization_id)
    try:
        store.record_progress(optimization_id, PROGRESS_BASELINE, {"score": 0.1})
        for i in range(3):
            store.record_progress(optimization_id, "step", {PROGRESS_EVENT_KEY: f"step:{i}"})
        events = store.get_progress_events(optimization_id)
        assert [e["event"] for e in events] == [PROGRESS_BASELINE, "step", "step", "step"]
    finally:
        store.delete_jobs([optimization_id])


def test_claim_orders_by_user_in_flight_then_age(store: RemoteDBJobStore) -> None:
    """Live-SQL check of the fairness ordering: fewest-in-flight user first.

    Exercises the correlated ``ORDER BY`` subquery + ``FOR UPDATE SKIP
    LOCKED`` combination on the real dialect. Alice's burst is older, but
    once her first job is in flight, Bob's younger job must claim next;
    Alice's second job follows. Runs after the drain test so no stray
    pending rows can interleave with the exact-order assertions.
    """
    prefix = f"fairness-test-{uuid.uuid4().hex[:8]}"
    ids = [f"{prefix}-a1", f"{prefix}-a2", f"{prefix}-b1"]
    for oid, username in zip(ids, (f"{prefix}-alice", f"{prefix}-alice", f"{prefix}-bob"), strict=True):
        store.create_job(oid, username=username)
        store.update_job(oid, payload={"ok": True})

    try:
        claimed = []
        for _ in range(3):
            row = store.claim_next_job("pod-fair", lease_seconds=60.0)
            assert row is not None
            claimed.append(row["optimization_id"])

        assert claimed == [f"{prefix}-a1", f"{prefix}-b1", f"{prefix}-a2"]
    finally:
        store.delete_jobs(ids)


@pytest.fixture
def trial_job(store: RemoteDBJobStore) -> Iterator[str]:
    """Create one job for trial rows and delete it, with its rows, after the test.

    Args:
        store: The live store.

    Yields:
        The job id.
    """
    job_id = f"trials-test-{uuid.uuid4().hex[:8]}"
    store.create_job(job_id)
    try:
        yield job_id
    finally:
        store.delete_jobs([job_id])


async def _keys(keys: list[str]) -> AsyncIterator[str]:
    """Yield the trial keys in order.

    Args:
        keys: The trial keys.

    Yields:
        The next key.
    """
    for key in keys:
        yield key


def _trial_work(ran: list[str]) -> BoxWork[str, str]:
    """Build box work that makes in-memory boxes and records each trial that runs.

    Args:
        ran: The list that gets each trial key that runs.

    Returns:
        The box work.
    """

    async def run(env: SandboxEnvironment, key: str) -> str:
        """Record the trial and return its result.

        Args:
            env: The box.
            key: The trial key.

        Returns:
            The result.
        """
        ran.append(key)
        return f"result-{key}"

    return BoxWork(
        create=lambda _key: InMemorySandbox(SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=1))),
        run=run,
    )


async def _ignore(outcome: BoxOutcome[str, str]) -> None:
    """Drop the outcome.

    Args:
        outcome: The outcome of one box.
    """


def test_restart_runs_only_the_trials_that_are_not_done(store: RemoteDBJobStore, trial_job: str) -> None:
    """Register 6 trials, finish 3, then resume: the second run starts only the other 3."""
    keys = [f"trial-{i}" for i in range(6)]
    trials = store.sandbox_trials
    trials.register_trials(trial_job, [(key, "fp-1") for key in keys])
    binding: DurableTrials[str, str] = DurableTrials(
        trials, trial_job, "worker-a", fingerprint=lambda _key: "fp-1", result_ref=lambda result: {"ref": result}
    )
    first: list[str] = []
    # The first run stops after 3 trials, as a worker crash would.
    asyncio.run(SandboxPool({"fake": 2}).run(_keys(keys[:3]), _trial_work(first), _ignore, key=str, trials=binding))
    trials.register_trials(trial_job, [(key, "fp-1") for key in keys])
    second: list[str] = []
    asyncio.run(SandboxPool({"fake": 2}).run(_keys(keys), _trial_work(second), _ignore, key=str, trials=binding))

    assert sorted(first) == keys[:3]
    assert sorted(second) == keys[3:]
    rows = trials.list_trials(trial_job)
    assert [(row["status"], row["attempt"]) for row in rows] == [(TrialStatus.SUCCEEDED.value, 1)] * 6
    assert rows[0]["result_ref"] == {"ref": "result-trial-0"}
    assert rows[0]["claimed_by"] == "worker-a"


def test_two_workers_claim_each_trial_exactly_once(store: RemoteDBJobStore, trial_job: str) -> None:
    """Let ten workers claim 30 trials in random orders: each trial gets exactly one claim."""
    keys = [f"trial-{i:02d}" for i in range(30)]
    trials = store.sandbox_trials
    trials.register_trials(trial_job, [(key, "fp") for key in keys])

    def claim_all(worker: int) -> list[str]:
        """Try to claim every trial in a random order.

        Args:
            worker: The worker number.

        Returns:
            The keys that this worker claimed.
        """
        order = random.Random(worker).sample(keys, len(keys))
        return [key for key in order if trials.claim_trial(trial_job, key, "fp", f"worker-{worker}") is not None]

    with ThreadPoolExecutor(max_workers=10) as pool:
        claimed = [key for keys_of_worker in pool.map(claim_all, range(10)) for key in keys_of_worker]

    assert sorted(claimed) == keys
    assert {row["status"] for row in trials.list_trials(trial_job)} == {TrialStatus.RUNNING.value}


def test_fingerprint_mismatch_fails_loud(store: RemoteDBJobStore, trial_job: str) -> None:
    """Raise on register and on claim when a trial's config changes, and save no new rows."""
    trials = store.sandbox_trials
    trials.register_trials(trial_job, [("trial-0", "fp-old")])

    with pytest.raises(TrialFingerprintError):
        trials.register_trials(trial_job, [("trial-0", "fp-new"), ("trial-1", "fp-new")])
    with pytest.raises(TrialFingerprintError):
        trials.claim_trial(trial_job, "trial-0", "fp-new", "worker-a")
    with pytest.raises(LookupError):
        trials.claim_trial(trial_job, "trial-9", "fp-new", "worker-a")

    rows = trials.list_trials(trial_job)
    assert [(row["trial_key"], row["status"], row["attempt"]) for row in rows] == [("trial-0", "pending", 0)]


def test_retryable_failed_trial_is_claimed_again_with_the_next_attempt(store: RemoteDBJobStore, trial_job: str) -> None:
    """Claim a retryable_failed row again with attempt 2, and never claim a failed or succeeded row."""
    trials = store.sandbox_trials
    trials.register_trials(trial_job, [("retry", "fp"), ("failed", "fp"), ("done", "fp")])
    for key in ("retry", "failed", "done"):
        assert trials.claim_trial(trial_job, key, "fp", "worker-a").attempt == 1
    assert trials.finish_trial(trial_job, "retry", 1, TrialStatus.RETRYABLE_FAILED, error="flake")
    assert trials.finish_trial(trial_job, "failed", 1, TrialStatus.FAILED, error="broken")
    assert trials.finish_trial(trial_job, "done", 1, TrialStatus.SUCCEEDED, result_ref={"ref": "r"})
    # A late write from an old attempt must not change the row.
    assert not trials.finish_trial(trial_job, "done", 1, TrialStatus.FAILED, error="late")

    assert trials.claim_trial(trial_job, "retry", "fp", "worker-b").attempt == 2
    assert trials.claim_trial(trial_job, "failed", "fp", "worker-b") is None
    assert trials.claim_trial(trial_job, "done", "fp", "worker-b") is None
    rows = {row["trial_key"]: row for row in trials.list_trials(trial_job)}
    assert rows["done"]["status"] == "succeeded"
    assert rows["done"]["result_ref"] == {"ref": "r"}
    assert rows["failed"]["error"] == "broken"


def test_deleting_the_job_deletes_its_trial_rows(store: RemoteDBJobStore) -> None:
    """Delete the trial rows with their job."""
    job_id = f"trials-test-{uuid.uuid4().hex[:8]}"
    store.create_job(job_id)
    store.sandbox_trials.register_trials(job_id, [("trial-0", "fp")])
    store.delete_jobs([job_id])
    assert store.sandbox_trials.list_trials(job_id) == []


def test_cancel_mid_trial_returns_it_and_resume_runs_exactly_it(store: RemoteDBJobStore, trial_job: str) -> None:
    """Cancel a run while one trial runs: the row goes back to the queue and the resume runs only it."""
    keys = ["t0", "t1", "t2"]
    trials = store.sandbox_trials
    trials.register_trials(trial_job, [(key, "fp") for key in keys])
    binding: DurableTrials[str, str] = DurableTrials(trials, trial_job, "worker-a", fingerprint=lambda _key: "fp")
    done: list[str] = []

    async def cancel_mid_trial() -> None:
        """Start the run, then cancel it once t0 and t2 are done and t1 runs."""
        t1_runs = asyncio.Event()
        others_done = asyncio.Event()

        async def hang_on_t1(env: SandboxEnvironment, key: str) -> str:
            """Return the result, and wait for ever on ``t1``.

            Args:
                env: The box.
                key: The trial key.

            Returns:
                The result.
            """
            if key == "t1":
                t1_runs.set()
                await asyncio.Event().wait()
            return f"result-{key}"

        async def record(outcome: BoxOutcome[str, str]) -> None:
            """Record a finished trial, and say when both other trials are done.

            Args:
                outcome: The outcome of one box.
            """
            done.append(outcome.candidate)
            if len(done) == 2:
                others_done.set()

        work = BoxWork(
            create=lambda _key: InMemorySandbox(SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=1))),
            run=hang_on_t1,
        )
        run = asyncio.create_task(SandboxPool({"fake": 3}).run(_keys(keys), work, record, key=str, trials=binding))
        async with asyncio.timeout(10):
            await t1_runs.wait()
            await others_done.wait()
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run

    asyncio.run(cancel_mid_trial())
    assert sorted(done) == ["t0", "t2"]
    rows = {row["trial_key"]: row for row in trials.list_trials(trial_job)}
    assert (rows["t1"]["status"], rows["t1"]["attempt"], rows["t1"]["interrupted"]) == ("retryable_failed", 1, 1)

    second: list[str] = []
    asyncio.run(SandboxPool({"fake": 3}).run(_keys(keys), _trial_work(second), _ignore, key=str, trials=binding))
    assert second == ["t1"]
    rows = {row["trial_key"]: row for row in trials.list_trials(trial_job)}
    assert {key: (row["status"], row["attempt"]) for key, row in rows.items()} == {
        "t0": ("succeeded", 1),
        "t1": ("succeeded", 2),
        "t2": ("succeeded", 1),
    }


def _fund_checkpoint(store: RemoteDBJobStore, job_id: str, monkeypatch: pytest.MonkeyPatch) -> BudgetService:
    """Attach a funded budget and a real checkpoint to a job, so a resume admits it.

    Args:
        store: The live store.
        job_id: The job to make recoverable.
        monkeypatch: Binds the current sandbox profile.

    Returns:
        The budget service for the resume.
    """
    monkeypatch.setattr(remote_mod.settings, "vercel_sandbox_max_lifetime_seconds", 60.0)
    username = f"trials-owner-{uuid.uuid4().hex[:8]}"
    with Session(store.engine) as session:
        session.add(
            BillingCustomerModel(
                username=username, stripe_customer_id=f"local-{username}", balance_cents=50, grant_remaining=0
            )
        )
        session.commit()
    service = BudgetService(engine=store.engine)
    budget = service.create(username, 20, idempotency_key=job_id)
    service.attach_to_job(budget.id, username, job_id, expected_revision=budget.revision)
    payload = {
        "optimizer_name": "GEPA",
        "dataset": [{"x": "sample"}],
        "execution_runtime": "vercel",
        "execution_budget_id": budget.id,
        "execution_budget_generation": 0,
    }
    store.update_job(
        job_id, username=username, payload=payload, execution_budget_id=budget.id, execution_budget_generation=0
    )
    state = GEPAState({"predict": "seed"}, ValsetEvaluation({0: "answer"}, {0: 0.5}))
    state.i = 3
    state.total_num_evals = 12
    data = pickle.dumps(state.__dict__)
    manifest = checkpoint_manifest(data, payload, store.get_job(job_id)["code_version"])
    store.save_gepa_checkpoint(job_id, data, 3, manifest=manifest)
    return service


def test_resume_claims_a_trial_that_a_crash_left_running(
    store: RemoteDBJobStore, trial_job: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reset a row that a hard crash left running, so the resume runs exactly that trial again."""
    keys = ["t0", "t1", "t2"]
    trials = store.sandbox_trials
    trials.register_trials(trial_job, [(key, "fp") for key in keys])
    for key in keys:
        assert trials.claim_trial(trial_job, key, "fp", "worker-old").attempt == 1
    assert trials.finish_trial(trial_job, "t0", 1, TrialStatus.SUCCEEDED)
    assert trials.finish_trial(trial_job, "t2", 1, TrialStatus.SUCCEEDED)
    # The worker dies here: t1 stays running and no cancel handler writes it back.
    service = _fund_checkpoint(store, trial_job, monkeypatch)
    store.update_job(trial_job, status="failed")

    assert store.requeue_for_resume(trial_job, budget_service=service) is not None
    rows = {row["trial_key"]: row for row in trials.list_trials(trial_job)}
    assert (rows["t1"]["status"], rows["t1"]["attempt"], rows["t1"]["interrupted"]) == ("retryable_failed", 1, 1)
    assert rows["t0"]["status"] == rows["t2"]["status"] == "succeeded"
    # A late write of the old attempt cannot land after the reset.
    assert not trials.finish_trial(trial_job, "t1", 1, TrialStatus.SUCCEEDED)

    binding: DurableTrials[str, str] = DurableTrials(trials, trial_job, "worker-new", fingerprint=lambda _key: "fp")
    ran: list[str] = []
    asyncio.run(SandboxPool({"fake": 3}).run(_keys(keys), _trial_work(ran), _ignore, key=str, trials=binding))
    assert ran == ["t1"]
    row = {row["trial_key"]: row for row in trials.list_trials(trial_job)}["t1"]
    assert (row["status"], row["attempt"], row["claimed_by"]) == ("succeeded", 2, "worker-new")

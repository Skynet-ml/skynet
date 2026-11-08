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
import random
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from core.config import app_env
from core.sandbox import ResourceRequest, SandboxEnvironment, SandboxSpec
from core.sandbox.pool import BoxOutcome, BoxWork, DurableTrials, SandboxPool, TrialFingerprintError, TrialStatus
from core.sandbox.tests.fake_provider import InMemorySandbox
from core.storage.remote import RemoteDBJobStore

TEST_DB_URL = app_env("APP_TEST_DB_URL")
BACKEND_DIR = Path(__file__).resolve().parents[3]

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


@pytest.fixture(scope="module")
def store() -> Iterator[RemoteDBJobStore]:
    """Yield a single store backed by the real Postgres for the whole module.

    The live database is upgraded to Alembic head first so model changes
    such as new indexed columns are present before rows are inserted.
    The store is reused across tests to avoid repeating that setup work.
    """
    _reset_legacy_schema(TEST_DB_URL or "")

    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DB_URL or "")
    command.upgrade(alembic_cfg, "head")

    s = RemoteDBJobStore(TEST_DB_URL)
    try:
        yield s
    finally:
        s.engine.dispose()


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
        assert trials.claim_trial(trial_job, key, "fp", "worker-a") == 1
    assert trials.finish_trial(trial_job, "retry", 1, TrialStatus.RETRYABLE_FAILED, error="flake")
    assert trials.finish_trial(trial_job, "failed", 1, TrialStatus.FAILED, error="broken")
    assert trials.finish_trial(trial_job, "done", 1, TrialStatus.SUCCEEDED, result_ref={"ref": "r"})
    # A late write from an old attempt must not change the row.
    assert not trials.finish_trial(trial_job, "done", 1, TrialStatus.FAILED, error="late")

    assert trials.claim_trial(trial_job, "retry", "fp", "worker-b") == 2
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

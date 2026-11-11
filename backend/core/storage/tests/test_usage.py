"""Tests for unified per-user storage accounting (``core.storage.usage``).

Runs against an in-memory SQLite engine (the sibling job-store tests' pattern: a
``RemoteDBJobStore`` subclass that skips the pgvector bootstrap so
``Base.metadata.create_all`` stands up every table). Covers the compact-JSON
sizer, the empty-user fast path, and the ``jobs.stored_bytes`` write path that
the optimizations category sums.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.api.errors import DomainError
from core.api.routers._helpers import enforce_storage_quota
from core.config import settings
from core.storage import remote as remote_store
from core.storage.models import (
    EMBEDDING_DIM,
    AgentStagedDatasetModel,
    Base,
    BillingCustomerModel,
    DatasetModel,
    JobEmbeddingModel,
    LogEntryModel,
    RunCallModel,
    TaggingSessionModel,
)
from core.storage.remote import RemoteDBJobStore
from core.storage.usage import (
    STORAGE_CATEGORIES,
    compute_user_storage,
    compute_user_storage_category_items,
    compute_user_storage_items,
    json_byte_size,
    purge_expired_staged_datasets,
)


class _SQLiteJobStore(RemoteDBJobStore):
    """RemoteDBJobStore on in-memory SQLite (skips the pgvector bootstrap)."""

    def __init__(self) -> None:
        """Build an in-memory SQLite engine and create the ORM tables."""
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)


@pytest.fixture
def store() -> Iterator[_SQLiteJobStore]:
    """Yield a fresh SQLite-backed store and drop its tables afterwards."""
    s = _SQLiteJobStore()
    yield s
    Base.metadata.drop_all(s.engine)


def test_json_byte_size_none_is_zero() -> None:
    """``json_byte_size(None)`` is 0 — an absent JSON column costs nothing."""
    assert json_byte_size(None) == 0


def test_json_byte_size_matches_compact_utf8_length() -> None:
    """``json_byte_size`` measures the compact-JSON UTF-8 byte length."""
    value = {"q": "שלום", "a": 4}
    expected = len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))
    assert json_byte_size(value) == expected


def test_compute_user_storage_empty_user_is_all_zero(store: _SQLiteJobStore) -> None:
    """A user with nothing stored totals zero with every category present."""
    usage = compute_user_storage(store.engine, "ghost")
    assert usage.total == 0
    assert set(usage.breakdown) == set(STORAGE_CATEGORIES)
    assert all(value == 0 for value in usage.breakdown.values())


def test_compute_user_storage_blank_username_short_circuits(store: _SQLiteJobStore) -> None:
    """A blank username returns the all-zero usage without scanning tables."""
    usage = compute_user_storage(store.engine, "   ")
    assert usage.total == 0


def test_stored_bytes_tracks_payload_and_result(store: _SQLiteJobStore) -> None:
    """Writing payload/result sets ``stored_bytes`` and feeds the optimizations total."""
    store.create_job("job-1", username="alice")
    payload = {"signature_code": "x", "rows": [{"q": "1", "a": "1"}]}
    result = {"score": 0.9}
    store.update_job("job-1", payload=payload, result=result)

    expected = json_byte_size(payload) + json_byte_size(result) + json_byte_size({})
    usage = compute_user_storage(store.engine, "alice")
    assert usage.breakdown["optimizations"] == expected
    assert usage.total == expected


def test_set_payload_overview_folds_overview_into_stored_bytes(store: _SQLiteJobStore) -> None:
    """``set_payload_overview`` recomputes ``stored_bytes`` to include the overview."""
    store.create_job("job-2", username="alice")
    store.update_job("job-2", payload={"a": 1})
    overview = {"name": "My run"}
    store.set_payload_overview("job-2", overview)

    expected = json_byte_size({"a": 1}) + json_byte_size(None) + json_byte_size(overview)
    assert compute_user_storage(store.engine, "alice").breakdown["optimizations"] == expected


def test_compute_user_storage_is_owner_scoped(store: _SQLiteJobStore) -> None:
    """One user's job bytes never leak into another user's total."""
    store.create_job("job-a", username="alice")
    store.update_job("job-a", payload={"owner": "alice"})
    store.create_job("job-b", username="bob")
    store.update_job("job-b", payload={"owner": "bob"})

    alice = compute_user_storage(store.engine, "alice")
    expected = json_byte_size({"owner": "alice"}) + json_byte_size({})
    assert alice.breakdown["optimizations"] == expected
    assert alice.total == alice.breakdown["optimizations"]


def test_byproducts_fold_into_optimization_footprint(store: _SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Logs and embeddings count toward the owning optimization, not a category of their own."""
    # job_embeddings exists (and thus has rows to count) only on the semantic
    # backend; usage accounting now skips it when embeddings are disabled, so
    # pin the flag on for the embedding-bytes assertion below. The SQLite test
    # store creates every table regardless, which is why this must be explicit.
    monkeypatch.setattr(settings, "embeddings_enabled", True)
    store.create_job("job-1", username="alice")
    store.update_job("job-1", payload={"a": 1})

    base = compute_user_storage(store.engine, "alice").breakdown["optimizations"]
    base_item = compute_user_storage_category_items(store.engine, "alice", "optimizations")[0]
    assert base_item.bytes == base

    message = "x" * 100
    with store._session_factory() as session:
        session.add(LogEntryModel(optimization_id="job-1", level="INFO", logger="t", message=message))
        session.add(JobEmbeddingModel(optimization_id="job-1", user_id="alice"))
        session.commit()

    embedding_bytes = EMBEDDING_DIM * 4 * 3
    usage = compute_user_storage(store.engine, "alice")
    assert set(usage.breakdown) == set(STORAGE_CATEGORIES)
    assert usage.breakdown["optimizations"] == base + len(message) + embedding_bytes
    assert usage.total == usage.breakdown["optimizations"]

    item = compute_user_storage_category_items(store.engine, "alice", "optimizations")[0]
    assert item.bytes == usage.breakdown["optimizations"]


def test_run_calls_fold_into_optimization_footprint(store: _SQLiteJobStore) -> None:
    """Stored relay-call text counts toward the owning optimization; other users' calls do not."""
    store.create_job("job-1", username="alice")
    store.update_job("job-1", payload={"a": 1})
    store.create_job("job-2", username="bob")

    base = compute_user_storage(store.engine, "alice").breakdown["optimizations"]

    request, response, target, failure = "q" * 300, "r" * 200, "https://api.example.test/v1", "timeout"
    with store._session_factory() as session:
        session.add(
            RunCallModel(
                optimization_id="job-1",
                candidate_id="c1",
                seq=1,
                kind="model",
                target=target,
                started_at=datetime.now(UTC),
                status="failed",
                failure_reason=failure,
                request=request,
                request_bytes=10_000,
                response=response,
                response_bytes=10_000,
            )
        )
        session.add(
            RunCallModel(
                optimization_id="job-1",
                candidate_id="c1",
                seq=2,
                kind="model",
                started_at=datetime.now(UTC),
                status="ok",
                request="a" * 50,
            )
        )
        session.add(
            RunCallModel(
                optimization_id="job-2",
                candidate_id="c1",
                seq=1,
                kind="model",
                started_at=datetime.now(UTC),
                status="ok",
                request="b" * 999,
            )
        )
        session.commit()

    expected = base + len(request) + len(response) + len(target) + len(failure) + 50
    usage = compute_user_storage(store.engine, "alice")
    assert usage.breakdown["optimizations"] == expected
    assert usage.total == expected

    item = compute_user_storage_category_items(store.engine, "alice", "optimizations")[0]
    assert item.bytes == expected


def _insert_dataset(store: _SQLiteJobStore, *, dataset_id: str, owner: str, name: str, byte_size: int) -> None:
    """Insert a minimal dataset row directly for storage-items ranking tests."""
    with store._session_factory() as session:
        session.add(
            DatasetModel(
                id=dataset_id,
                owner_username=owner,
                name=name,
                source="upload",
                row_count=1,
                column_count=1,
                byte_size=byte_size,
                stored_bytes=byte_size,
                content_hash=dataset_id,
                column_schema={},
            )
        )
        session.commit()


def test_storage_items_empty_user_is_empty(store: _SQLiteJobStore) -> None:
    """A user with nothing stored ranks to an empty list."""
    assert compute_user_storage_items(store.engine, "ghost") == []


def test_storage_items_blank_username_short_circuits(store: _SQLiteJobStore) -> None:
    """A blank username returns no items without scanning tables."""
    assert compute_user_storage_items(store.engine, "   ") == []


def test_storage_items_rank_across_types_by_size(store: _SQLiteJobStore) -> None:
    """Optimizations and datasets merge into one list ordered by descending size."""
    store.create_job("job-small", username="alice")
    store.update_job("job-small", payload={"a": 1})
    store.set_payload_overview("job-small", {"name": "Small run"})
    store.create_job("job-big", username="alice")
    store.update_job("job-big", payload={"rows": list(range(50))})
    store.set_payload_overview("job-big", {"name": "Big run"})
    _insert_dataset(store, dataset_id="ds-1", owner="alice", name="Mid dataset", byte_size=200)

    items = compute_user_storage_items(store.engine, "alice")
    assert [item.bytes for item in items] == sorted((item.bytes for item in items), reverse=True)
    by_id = {item.id: item for item in items}
    assert by_id["job-big"].type == "optimization"
    assert by_id["job-big"].name == "Big run"
    assert by_id["ds-1"].type == "dataset"
    assert by_id["ds-1"].bytes == 200


def test_storage_items_are_owner_scoped(store: _SQLiteJobStore) -> None:
    """One user's items never surface in another user's ranking."""
    _insert_dataset(store, dataset_id="ds-alice", owner="alice", name="Alice set", byte_size=100)
    _insert_dataset(store, dataset_id="ds-bob", owner="bob", name="Bob set", byte_size=999)

    alice_ids = {item.id for item in compute_user_storage_items(store.engine, "alice")}
    assert alice_ids == {"ds-alice"}


def test_storage_items_honours_limit(store: _SQLiteJobStore) -> None:
    """The merged ranking is capped at ``limit`` items."""
    for index in range(5):
        _insert_dataset(store, dataset_id=f"ds-{index}", owner="alice", name=f"Set {index}", byte_size=(index + 1) * 10)

    items = compute_user_storage_items(store.engine, "alice", limit=2)
    assert len(items) == 2
    assert [item.bytes for item in items] == [50, 40]


def _insert_staged(store: _SQLiteJobStore, *, staged_id: str, username: str, filename: str, rows: list) -> None:
    """Insert a staged (pending) upload row directly for category-listing tests."""
    with store._session_factory() as session:
        session.add(
            AgentStagedDatasetModel(
                id=staged_id,
                username=username,
                dataset_filename=filename,
                rows=rows,
                row_count=len(rows),
            )
        )
        session.commit()


def test_purge_expired_staged_datasets_drops_only_rows_past_ttl(store: _SQLiteJobStore) -> None:
    """The TTL sweep deletes staged rows older than the cutoff and spares fresh ones."""
    now = datetime.now(UTC)
    with store._session_factory() as session:
        session.add(
            AgentStagedDatasetModel(
                id="stale",
                username="alice",
                dataset_filename="old.csv",
                rows=[{"q": "a"}],
                row_count=1,
                created_at=now - timedelta(minutes=20),
            )
        )
        session.add(
            AgentStagedDatasetModel(
                id="fresh",
                username="alice",
                dataset_filename="new.csv",
                rows=[{"q": "b"}],
                row_count=1,
                created_at=now - timedelta(minutes=2),
            )
        )
        session.commit()

    deleted = purge_expired_staged_datasets(store.engine, max_age_seconds=600)

    assert deleted == 1
    with store._session_factory() as session:
        surviving = {row.id for row in session.query(AgentStagedDatasetModel).all()}
    assert surviving == {"fresh"}


def test_category_items_blank_username_is_empty(store: _SQLiteJobStore) -> None:
    """A blank username returns no items without scanning tables."""
    assert compute_user_storage_category_items(store.engine, "  ", "datasets") == []


def test_category_items_non_deletable_category_is_empty(store: _SQLiteJobStore) -> None:
    """A byproduct category (no standalone artifact) yields an empty list."""
    store.create_job("job-x", username="alice")
    store.update_job("job-x", payload={"a": 1})
    assert compute_user_storage_category_items(store.engine, "alice", "embeddings") == []
    assert compute_user_storage_category_items(store.engine, "alice", "bogus") == []


def test_category_items_lists_all_datasets_largest_first(store: _SQLiteJobStore) -> None:
    """The datasets category returns every dataset, ordered by descending size."""
    _insert_dataset(store, dataset_id="ds-small", owner="alice", name="Small", byte_size=10)
    _insert_dataset(store, dataset_id="ds-big", owner="alice", name="Big", byte_size=900)
    _insert_dataset(store, dataset_id="ds-mid", owner="alice", name="Mid", byte_size=100)

    items = compute_user_storage_category_items(store.engine, "alice", "datasets")
    assert [item.id for item in items] == ["ds-big", "ds-mid", "ds-small"]
    assert all(item.type == "dataset" for item in items)


def test_category_items_lists_optimizations(store: _SQLiteJobStore) -> None:
    """The optimizations category returns the user's jobs by stored bytes."""
    store.create_job("job-1", username="alice")
    store.update_job("job-1", payload={"rows": list(range(20))})
    store.set_payload_overview("job-1", {"name": "My run"})

    items = compute_user_storage_category_items(store.engine, "alice", "optimizations")
    assert len(items) == 1
    assert items[0].id == "job-1"
    assert items[0].type == "optimization"
    assert items[0].name == "My run"
    assert items[0].bytes > 0


def test_category_items_lists_staged_uploads(store: _SQLiteJobStore) -> None:
    """The staged_uploads category returns pending uploads named by filename."""
    _insert_staged(store, staged_id="st-1", username="alice", filename="data.csv", rows=[{"q": "1"}])

    items = compute_user_storage_category_items(store.engine, "alice", "staged_uploads")
    assert len(items) == 1
    assert items[0].id == "st-1"
    assert items[0].type == "staged_upload"
    assert items[0].name == "data.csv"
    assert items[0].bytes > 0


def _insert_tagging_session(store: _SQLiteJobStore, *, session_id: str, username: str, name: str, rows: list) -> None:
    """Insert a labeling session row directly for storage-accounting tests."""
    with store._session_factory() as session:
        session.add(
            TaggingSessionModel(
                id=session_id,
                username=username,
                name=name,
                config={},
                columns=["q"],
                data=rows,
                annotations={"0": "yes"},
                row_count=len(rows),
            )
        )
        session.commit()


def test_tagging_sessions_count_toward_storage(store: _SQLiteJobStore) -> None:
    """A labeling session's rows and labels land in their own breakdown category and the total."""
    _insert_tagging_session(store, session_id="ts-1", username="alice", name="Labels", rows=[{"q": "x" * 200}])

    usage = compute_user_storage(store.engine, "alice")
    assert usage.breakdown["tagging_sessions"] > 200
    assert usage.total == sum(usage.breakdown.values())
    assert usage.counts["tagging_sessions"] == 1
    assert compute_user_storage(store.engine, "bob").breakdown["tagging_sessions"] == 0


def test_category_items_lists_tagging_sessions(store: _SQLiteJobStore) -> None:
    """The tagging_sessions category lists sessions whose sizes sum to the category total."""
    _insert_tagging_session(store, session_id="ts-small", username="alice", name="Small", rows=[{"q": "1"}])
    _insert_tagging_session(store, session_id="ts-big", username="alice", name="Big", rows=[{"q": "y" * 500}])

    items = compute_user_storage_category_items(store.engine, "alice", "tagging_sessions")
    assert [item.id for item in items] == ["ts-big", "ts-small"]
    assert {item.type for item in items} == {"tagging_session"}
    assert (
        sum(item.bytes for item in items) == compute_user_storage(store.engine, "alice").breakdown["tagging_sessions"]
    )


def test_storage_counts_cover_every_category(store: _SQLiteJobStore) -> None:
    """Item counts are reported for every category, matching what was stored."""
    _insert_dataset(store, dataset_id="ds-1", owner="alice", name="Set", byte_size=10)
    _insert_staged(store, staged_id="st-1", username="alice", filename="a.csv", rows=[{"q": "1"}])

    counts = compute_user_storage(store.engine, "alice").counts
    assert set(counts) == set(STORAGE_CATEGORIES)
    assert counts["datasets"] == 1
    assert counts["staged_uploads"] == 1
    assert counts["optimizations"] == 0


def test_category_items_are_owner_scoped(store: _SQLiteJobStore) -> None:
    """One user's category items never surface in another user's listing."""
    _insert_dataset(store, dataset_id="ds-alice", owner="alice", name="Alice set", byte_size=100)
    _insert_dataset(store, dataset_id="ds-bob", owner="bob", name="Bob set", byte_size=999)

    alice_ids = {item.id for item in compute_user_storage_category_items(store.engine, "alice", "datasets")}
    assert alice_ids == {"ds-alice"}


def test_storage_quota_override_absent_returns_none(store: _SQLiteJobStore) -> None:
    """A user with no override row resolves to ``None``."""
    assert store.get_user_storage_quota_override("alice") is None


def test_effective_storage_quota_falls_back_to_default(store: _SQLiteJobStore) -> None:
    """Without an override the effective budget is the global default."""
    assert store.get_effective_user_storage_quota("alice") == settings.user_storage_quota_bytes


def test_local_mode_budget_is_used_bytes_plus_free_disk(
    store: _SQLiteJobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Mac app ignores the 500 MB web budget and admin overrides (SKY-344)."""
    two_gb = 2 * 1024 * 1024 * 1024
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(settings, "local_state_dir", str(tmp_path))
    monkeypatch.setattr(remote_store, "_local_postgres_free_bytes", lambda: None)
    measured: list[Path] = []
    monkeypatch.setattr(shutil, "disk_usage", lambda path: measured.append(path) or SimpleNamespace(free=two_gb))
    store.set_user_storage_quota_override("alice", 1, updated_by="admin")
    _insert_dataset(store, dataset_id="ds-1", owner="alice", name="Set", byte_size=600 * 1024 * 1024)

    budget = store.get_effective_user_storage_quota("alice")

    assert budget == 600 * 1024 * 1024 + two_gb
    assert budget > settings.user_storage_quota_bytes
    assert measured == [tmp_path]


def test_local_mode_gate_blocks_only_writes_larger_than_free_disk(
    store: _SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local save passes the 500 MB mark, and fails only when the disk is full."""
    free = 1024 * 1024 * 1024
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(settings, "local_state_dir", "")
    monkeypatch.setattr(remote_store, "_local_postgres_free_bytes", lambda: None)
    measured: list[Path] = []
    monkeypatch.setattr(shutil, "disk_usage", lambda path: measured.append(path) or SimpleNamespace(free=free))

    enforce_storage_quota(store, "alice", incoming_bytes=900 * 1024 * 1024)
    with pytest.raises(DomainError) as exc:
        enforce_storage_quota(store, "alice", incoming_bytes=free + 1)
    assert exc.value.code == "user.storage.quota_exceeded"
    assert set(measured) == {Path.home()}


def test_local_mode_budget_uses_the_smaller_docker_disk(
    store: _SQLiteJobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the Docker disk has less room than the Mac disk, the budget shows the Docker room."""
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(settings, "local_state_dir", "")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=200 * 1024**3))
    monkeypatch.setattr(remote_store, "_local_postgres_free_bytes", lambda: 30 * 1024**3)

    assert store.get_effective_user_storage_quota("alice") == 30 * 1024**3


def test_local_postgres_free_bytes_reads_df_and_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Docker probe parses ``df`` output and returns None when Docker cannot answer."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="   Avail\n32212254720\n")

    monkeypatch.setattr(remote_store.shutil, "which", lambda name: "/usr/local/bin/docker")
    monkeypatch.setattr(remote_store.subprocess, "run", fake_run)
    assert remote_store._local_postgres_free_bytes() == 32212254720
    assert calls[0][:3] == ["/usr/local/bin/docker", "exec", "skynet-local-postgres"]

    monkeypatch.setattr(remote_store.subprocess, "run", lambda args, **_: SimpleNamespace(returncode=1, stdout=""))
    assert remote_store._local_postgres_free_bytes() is None

    monkeypatch.setattr(remote_store.shutil, "which", lambda name: None)
    assert remote_store._local_postgres_free_bytes() is None


def test_storage_quota_override_replaces_default(store: _SQLiteJobStore) -> None:
    """An override becomes the user's effective budget, case-insensitively."""
    five_gb = 5 * 1024 * 1024 * 1024
    store.set_user_storage_quota_override("Alice", five_gb, updated_by="admin")

    assert store.get_user_storage_quota_override("alice") == five_gb
    assert store.get_effective_user_storage_quota("alice") == five_gb

    rows = store.list_user_storage_quota_overrides()
    assert rows == [
        {"username": "alice", "quota_bytes": five_gb, "updated_at": rows[0]["updated_at"], "updated_by": "admin"}
    ]
    assert rows[0]["updated_at"] is not None


def test_storage_quota_override_can_go_below_default(store: _SQLiteJobStore) -> None:
    """An override may set a ceiling below the global default."""
    store.set_user_storage_quota_override("alice", 1024)
    assert store.get_effective_user_storage_quota("alice") == 1024


def test_storage_quota_override_rejects_non_positive(store: _SQLiteJobStore) -> None:
    """A byte budget below one is rejected at the store seam."""
    with pytest.raises(ValueError):
        store.set_user_storage_quota_override("alice", 0)


def test_delete_storage_quota_override_restores_default(store: _SQLiteJobStore) -> None:
    """Deleting an override restores the global default budget."""
    store.set_user_storage_quota_override("alice", 4096)
    assert store.delete_user_storage_quota_override("alice") is True
    assert store.get_user_storage_quota_override("alice") is None
    assert store.get_effective_user_storage_quota("alice") == settings.user_storage_quota_bytes
    assert store.delete_user_storage_quota_override("alice") is False


def _set_subscription_status(
    store: _SQLiteJobStore,
    username: str,
    status: str | None,
    past_due_since: datetime | None = None,
) -> None:
    """Seed a billing row whose mirrored subscription has ``status``.

    Args:
        store: SQLite-backed store.
        username: Account to seed.
        status: Mirrored Stripe subscription status.
        past_due_since: When the subscription went past due, if it did.
    """
    with store._get_session() as session:
        session.add(
            BillingCustomerModel(
                username=username,
                stripe_customer_id=f"cus_{username}",
                balance_cents=0,
                subscription_status=status,
                subscription_past_due_since=past_due_since,
            )
        )
        session.commit()


def test_pro_plan_lifts_storage_and_job_quota(store: _SQLiteJobStore) -> None:
    """An entitled Pro account gets the Pro storage budget and the Pro job cap."""
    _set_subscription_status(store, "alice", "active")
    assert store.has_pro_plan("Alice") is True
    assert store.get_effective_user_storage_quota("alice") == settings.pro_storage_quota_bytes
    assert store.get_effective_user_quota("alice") == settings.pro_max_jobs_per_user


def test_past_due_pro_plan_keeps_limits_inside_the_grace_period(store: _SQLiteJobStore) -> None:
    """A subscription that just went past due still grants Pro limits."""
    _set_subscription_status(store, "alice", "past_due", past_due_since=datetime.now(UTC) - timedelta(days=1))
    assert store.has_pro_plan("alice") is True
    assert store.get_effective_user_quota("alice") == settings.pro_max_jobs_per_user


def test_past_due_pro_plan_lapses_after_the_grace_period(store: _SQLiteJobStore) -> None:
    """Past the grace period a past-due subscription falls back to free limits."""
    since = datetime.now(UTC) - timedelta(days=settings.pro_past_due_grace_days + 1)
    _set_subscription_status(store, "alice", "past_due", past_due_since=since)
    assert store.has_pro_plan("alice") is False
    assert store.get_effective_user_storage_quota("alice") == settings.user_storage_quota_bytes
    assert store.get_effective_user_quota("alice") == settings.max_jobs_per_user


def test_lapsed_pro_plan_falls_back_to_defaults(store: _SQLiteJobStore) -> None:
    """A canceled subscription returns the account to the free limits."""
    _set_subscription_status(store, "alice", "canceled")
    assert store.has_pro_plan("alice") is False
    assert store.get_effective_user_storage_quota("alice") == settings.user_storage_quota_bytes
    assert store.get_effective_user_quota("alice") == settings.max_jobs_per_user


def test_admin_storage_override_beats_pro_plan(store: _SQLiteJobStore) -> None:
    """An admin override still decides the budget for a Pro account."""
    _set_subscription_status(store, "alice", "active")
    store.set_user_storage_quota_override("alice", 4096)
    assert store.get_effective_user_storage_quota("alice") == 4096

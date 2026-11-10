"""Tests for the run-scoped setup snapshot cache. Fake SDK clients only; no paid calls."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from vercel.sandbox import SandboxApiError, SnapshotSource

from core.billing.budget_amounts import cent_units
from core.billing.budgets import BudgetError, BudgetService
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.billing.vercel_usage import (
    SessionNotStoppedError,
    VercelSnapshotReservation,
    vercel_charge_policy,
    vercel_snapshot_actual_usd,
)
from core.config import app_env, settings
from core.sandbox.errors import SandboxFatalError, SandboxInfraError
from core.sandbox.providers.vercel import VercelEnvironment
from core.sandbox.resources import SandboxSpec
from core.sandbox.snapshots import (
    SNAPSHOT_SPEC_VERSION,
    BuiltSnapshot,
    SnapshotCache,
    SnapshotRefusedError,
    advisory_lock_key,
    refuse_secrets,
    snapshot_cache_key,
    snapshot_expiration,
    snapshot_hold_retention,
)
from core.sandbox.tests.test_provider_vercel import FakeBox, FakeClient, FakeClock, FakeSession, make_env
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

TEST_DB_URL = app_env("APP_BUDGET_TEST_DB_URL")
DEADLINE = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
KEY_INPUTS: dict[str, Any] = {
    "image_digest": "vcr.example/skynet/optimizer@sha256:" + "a" * 64,
    "setup_command": "git clone repo .",
    "install_command": "uv sync --frozen",
    "lock_files": {"uv.lock": "version = 1\n", "package-lock.json": b"{}"},
    "spec_version": SNAPSHOT_SPEC_VERSION,
}


def _key(**changes: Any) -> str:
    """Return the cache key of the base inputs with some changes.

    Args:
        changes: The inputs to replace.

    Returns:
        The cache key.
    """
    return snapshot_cache_key(**{**KEY_INPUTS, **changes})


def test_same_inputs_give_the_same_key() -> None:
    """Check that the key is stable and ignores the lock file order."""
    reordered = dict(reversed(list(KEY_INPUTS["lock_files"].items())))
    assert _key() == _key()
    assert _key() == _key(lock_files=reordered)


@pytest.mark.parametrize(
    "changes",
    [
        {"image_digest": "vcr.example/skynet/optimizer@sha256:" + "b" * 64},
        {"setup_command": "git clone other ."},
        {"install_command": "uv sync"},
        {"lock_files": {"uv.lock": "version = 2\n", "package-lock.json": b"{}"}},
        {"lock_files": {"uv.lock": "version = 1\n"}},
        {"lock_files": {"other/uv.lock": "version = 1\n", "package-lock.json": b"{}"}},
        {"spec_version": "skynet-setup-snapshot-v2"},
    ],
)
def test_each_changed_input_gives_a_new_key(changes: dict[str, Any]) -> None:
    """Check that every input is part of the key.

    Args:
        changes: The input to change.
    """
    assert _key(**changes) != _key()


def test_lock_key_is_a_signed_bigint_scoped_to_the_run() -> None:
    """Check that the advisory key fits Postgres and differs between runs."""
    key = advisory_lock_key("run-1", _key())
    assert -(2**63) <= key < 2**63
    assert key == advisory_lock_key("run-1", _key())
    assert key != advisory_lock_key("run-2", _key())


@pytest.mark.parametrize(
    "fields",
    [{"env": {"API_KEY": "fixture"}}, {"inject_headers": {"api.example.com": {"Authorization": "fixture"}}}],
)
def test_box_with_secrets_is_refused(fields: dict[str, Any]) -> None:
    """Check that env values and injected headers block a snapshot.

    Args:
        fields: The secret-carrying spec fields.
    """
    with pytest.raises(SnapshotRefusedError):
        refuse_secrets(SandboxSpec(lifetime_seconds=60, **fields))
    refuse_secrets(SandboxSpec(lifetime_seconds=60))


def test_expiry_is_the_deadline_plus_one_day() -> None:
    """Check the expiry, and its one-day floor after a passed deadline."""
    assert snapshot_expiration(DEADLINE, NOW) == timedelta(days=3)
    assert snapshot_expiration(DEADLINE, DEADLINE + timedelta(days=5)) == timedelta(days=1)


def test_hold_covers_a_last_use_at_the_deadline_plus_the_expiry() -> None:
    """Check that the hold covers the sliding expiry after the latest last use."""
    retention = snapshot_hold_retention(DEADLINE, NOW)
    assert retention == timedelta(days=5)
    assert NOW + retention == DEADLINE + snapshot_expiration(DEADLINE, NOW)
    late = DEADLINE + timedelta(days=5)
    assert snapshot_hold_retention(DEADLINE, late) == timedelta(days=1)


class Provider:
    """Record snapshot builds and deletes like a shared provider account."""

    def __init__(self) -> None:
        """Start with no snapshots."""
        self.live: dict[str, BuiltSnapshot] = {}
        self.builds: list[str] = []
        self.deletes: list[str] = []
        self.failures: list[BaseException] = []

    async def build(self, name: str) -> BuiltSnapshot:
        """Build one snapshot slowly, so a second builder overlaps it.

        Args:
            name: The builder box name.

        Returns:
            The new snapshot.
        """
        self.builds.append(name)
        await asyncio.sleep(0.2)
        if self.failures:
            raise self.failures.pop(0)
        built = BuiltSnapshot(snapshot_id=f"snap-{len(self.builds)}", builder_name=name)
        self.live[name] = built
        return built

    async def lookup(self, name: str) -> BuiltSnapshot | None:
        """Find a live snapshot by builder name.

        Args:
            name: The builder box name.

        Returns:
            The snapshot, or ``None``.
        """
        return self.live.get(name)

    async def delete(self, built: BuiltSnapshot) -> None:
        """Delete one snapshot.

        Args:
            built: The snapshot to delete.
        """
        self.deletes.append(built.snapshot_id)
        self.live.pop(built.builder_name, None)


async def test_concurrent_builders_in_one_process_build_once() -> None:
    """Check that callers of one key share a single build."""
    provider = Provider()
    cache = SnapshotCache("run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup)
    results = await asyncio.gather(*(cache.get_or_build(_key(), provider.build) for _ in range(5)))
    assert len(provider.builds) == 1
    assert {built.snapshot_id for built in results} == {"snap-1"}
    assert provider.builds == [cache.builder_name(_key())]


async def test_failed_build_is_not_retried_or_kept() -> None:
    """Check that a failed build raises once and leaves nothing cached."""
    provider = Provider()
    provider.failures.append(RuntimeError("setup failed"))
    cache = SnapshotCache("run-1", DEADLINE, delete=provider.delete)
    with pytest.raises(RuntimeError):
        await cache.get_or_build(_key(), provider.build)
    assert len(provider.builds) == 1
    await cache.close()
    assert provider.deletes == []


async def test_run_end_deletes_every_snapshot_and_refuses_new_builds() -> None:
    """Check that close deletes each snapshot and closes the cache."""
    provider = Provider()
    cache = SnapshotCache("run-1", DEADLINE, delete=provider.delete)
    await cache.get_or_build(_key(), provider.build)
    await cache.get_or_build(_key(install_command="uv sync"), provider.build)
    assert cache.expiration(NOW) == timedelta(days=3)
    await cache.close()
    assert sorted(provider.deletes) == ["snap-1", "snap-2"]
    assert provider.live == {}
    with pytest.raises(SandboxFatalError):
        await cache.get_or_build(_key(), provider.build)


async def test_failed_delete_is_kept_for_the_next_close() -> None:
    """Check that a failed delete raises and the next close tries again."""
    provider = Provider()
    calls: list[str] = []

    async def flaky_delete(built: BuiltSnapshot) -> None:
        """Fail the first delete, then delete.

        Args:
            built: The snapshot to delete.
        """
        calls.append(built.snapshot_id)
        if len(calls) == 1:
            raise RuntimeError("provider error")
        await provider.delete(built)

    cache = SnapshotCache("run-1", DEADLINE, delete=flaky_delete)
    await cache.get_or_build(_key(), provider.build)
    with pytest.raises(RuntimeError):
        await cache.close()
    await cache.close()
    assert provider.deletes == ["snap-1"]


async def test_idle_entry_leaves_the_cache_without_a_provider_delete() -> None:
    """Check that eviction drops only the idle entry and close still deletes it."""
    provider = Provider()
    clock = FakeClock()
    cache = SnapshotCache("run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup, clock=clock)
    await cache.get_or_build(_key(), provider.build)
    clock.now = 100.0
    await cache.get_or_build(_key(install_command="uv sync"), provider.build)
    clock.now = 150.0
    assert await cache.evict_idle(120.0) == [_key()]
    assert provider.deletes == []
    assert (await cache.get_or_build(_key(), provider.build)).snapshot_id == "snap-1"
    assert len(provider.builds) == 2
    await cache.close()
    assert sorted(provider.deletes) == ["snap-1", "snap-2"]


async def test_evicted_entry_without_lookup_is_rebuilt_and_both_are_deleted_at_close() -> None:
    """Check that a rebuilt key keeps the first snapshot for the run-end delete."""
    provider = Provider()
    clock = FakeClock()
    cache = SnapshotCache("run-1", DEADLINE, delete=provider.delete, clock=clock)
    await cache.get_or_build(_key(), provider.build)
    clock.now = 1_000.0
    assert await cache.evict_idle(600.0) == [_key()]
    assert (await cache.get_or_build(_key(), provider.build)).snapshot_id == "snap-2"
    assert provider.deletes == []
    await cache.close()
    assert sorted(provider.deletes) == ["snap-1", "snap-2"]


async def test_borrowed_snapshot_is_only_dropped_from_the_cache() -> None:
    """Check that a pod never deletes a snapshot that another pod built."""
    provider = Provider()
    owner_clock, borrower_clock = FakeClock(), FakeClock()
    owner = SnapshotCache("run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup, clock=owner_clock)
    borrower = SnapshotCache("run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup, clock=borrower_clock)
    built = await owner.get_or_build(_key(), provider.build)
    assert (await borrower.get_or_build(_key(), provider.build)).snapshot_id == built.snapshot_id
    borrower_clock.now = 1_000.0
    assert await borrower.evict_idle(600.0) == [_key()]
    await borrower.close()
    assert provider.deletes == []
    assert (await owner.get_or_build(_key(), provider.build)).snapshot_id == built.snapshot_id
    await owner.close()
    assert provider.deletes == [built.snapshot_id]
    assert len(provider.builds) == 1


async def test_owner_eviction_keeps_a_snapshot_that_a_borrower_still_uses() -> None:
    """Check that the owner's idle time never deletes a snapshot in use on another pod."""
    provider = Provider()
    owner_clock, borrower_clock = FakeClock(), FakeClock()
    owner = SnapshotCache("run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup, clock=owner_clock)
    borrower = SnapshotCache("run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup, clock=borrower_clock)
    built = await owner.get_or_build(_key(), provider.build)
    await borrower.get_or_build(_key(), provider.build)
    owner_clock.now = borrower_clock.now = 1_000.0
    await borrower.get_or_build(_key(), provider.build)
    assert await owner.evict_idle(600.0) == [_key()]
    assert provider.deletes == []
    assert provider.live[built.builder_name].snapshot_id == built.snapshot_id
    await owner.close()
    assert provider.deletes == [built.snapshot_id]


async def test_build_that_ends_after_close_is_deleted_and_refused() -> None:
    """Check that a snapshot built while the run closed is deleted, not returned."""
    provider = Provider()
    cache = SnapshotCache("run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup)
    build = asyncio.create_task(cache.get_or_build(_key(), provider.build))
    await asyncio.sleep(0.05)
    assert provider.builds
    assert not provider.live
    await cache.close()
    with pytest.raises(SandboxFatalError):
        await build
    assert provider.deletes == ["snap-1"]
    assert provider.live == {}
    await cache.close()
    assert provider.deletes == ["snap-1"]


async def test_late_build_whose_delete_fails_is_still_refused() -> None:
    """Check that a failed late delete raises the run-ended error."""
    provider = Provider()

    async def failing_delete(built: BuiltSnapshot) -> None:
        """Fail every delete.

        Args:
            built: The snapshot to delete.
        """
        raise RuntimeError("provider error")

    cache = SnapshotCache("run-1", DEADLINE, delete=failing_delete)
    build = asyncio.create_task(cache.get_or_build(_key(), provider.build))
    await asyncio.sleep(0.05)
    await cache.close()
    with pytest.raises(SandboxFatalError) as raised:
        await build
    assert isinstance(raised.value.__cause__, RuntimeError)


def _api_error(code: str) -> SandboxApiError:
    """Build an SDK error with one error code.

    Args:
        code: The Vercel error code.

    Returns:
        The SDK error.
    """
    return SandboxApiError(httpx.Response(400), code, data={"error": {"code": code}})


class FakeSnapshotUsage:
    """Record the snapshot reservation calls."""

    def __init__(self, log: list[str], request: dict[str, Any], operation_key: str) -> None:
        """Record the reservation.

        Args:
            log: The shared call log.
            request: The snapshot request.
            operation_key: The operation key.
        """
        self.log = log
        self.request = request
        self.operation_key = operation_key
        log.append("snapshot-reserve")

    def bind(self, snapshot_id: str) -> None:
        """Log the bound snapshot.

        Args:
            snapshot_id: The snapshot id.
        """
        self.log.append(f"snapshot-bind:{snapshot_id}")

    def pending(self, evidence: Any = None) -> None:
        """Log a pending hold.

        Args:
            evidence: Unused.
        """
        self.log.append("snapshot-pending")

    def settle(self, snapshot: Any) -> None:
        """Log a settled hold.

        Args:
            snapshot: The provider state after the delete.
        """
        self.log.append(f"snapshot-settle:{snapshot['status']}")


class FailingBindUsage(FakeSnapshotUsage):
    """Fail to record the snapshot id, like a lost database write."""

    def bind(self, snapshot_id: str) -> None:
        """Log the bind and fail.

        Args:
            snapshot_id: The snapshot id.

        Raises:
            RuntimeError: Always.
        """
        self.log.append(f"snapshot-bind-failed:{snapshot_id}")
        raise RuntimeError("database down")


def _snapshotting_env(log: list[str], client: FakeClient, **spec_fields: Any) -> VercelEnvironment:
    """Make a protected provider whose snapshot reservations are fakes.

    Args:
        log: The shared call log.
        client: The fake SDK client.
        spec_fields: Other spec fields.

    Returns:
        The provider.
    """
    env = make_env(client, log, **spec_fields)

    def factory(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeSnapshotUsage:
        """Make a fake snapshot reservation.

        Args:
            budget: Unused.
            request: The snapshot request.
            operation_key: The operation key.

        Returns:
            The fake reservation.
        """
        return FakeSnapshotUsage(log, request, operation_key)

    env._snapshot_usage_factory = factory
    return env


async def test_setup_box_becomes_a_snapshot_with_the_run_expiry() -> None:
    """Check the snapshot call, its hold, and that teardown keeps the builder box."""
    log: list[str] = []
    client = FakeClient(log)
    env = _snapshotting_env(log, client)
    await env.start()
    session = client.boxes[env.name].current_session
    expirations: list[Any] = []

    async def snapshot(*, expiration: Any = None) -> Any:
        """Stop the session and return a created snapshot.

        Args:
            expiration: The requested expiry.

        Returns:
            The snapshot handle.
        """
        expirations.append(expiration)
        return SimpleNamespace(id="snap-1", status="created", expires_at=1_000)

    session.snapshot = snapshot
    built = await env.snapshot(run_deadline=DEADLINE, now=NOW)
    assert expirations == [timedelta(days=3)]
    assert built.snapshot_id == "snap-1"
    assert built.builder_name == env.name
    # Two days to the deadline, then three days of expiry after a last use there.
    assert built.reservation.request == {"session_id": "session-1", "retention_ms": 5 * 86_400_000}
    assert built.reservation.operation_key == "op-1:snapshot"
    assert log.index("snapshot-reserve") < log.index("snapshot-bind:snap-1")
    with pytest.raises(SandboxInfraError):
        await env.exec("echo hi", timeout_sec=5)
    await env.stop()
    box = client.boxes[env.name]
    assert (box.stops, box.destroys, session.stops) == (0, 0, 0)
    assert log[-1] == "settle"


async def _snapshotted_env_whose_settle_raises(log: list[str], error: Exception) -> VercelEnvironment:
    """Make a box that took its snapshot and whose usage settle raises.

    Args:
        log: The shared call log.
        error: The error that the usage settle raises.

    Returns:
        The provider, ready to stop.
    """
    client = FakeClient(log)
    env = _snapshotting_env(log, client)
    await env.start()

    async def snapshot(*, expiration: Any = None) -> Any:
        """Return a created snapshot.

        Args:
            expiration: Unused.

        Returns:
            The snapshot handle.
        """
        return SimpleNamespace(id="snap-1", status="created", expires_at=1_000)

    def settle() -> None:
        """Raise the settle error.

        Raises:
            Exception: Always, the given error.
        """
        raise error

    client.boxes[env.name].current_session.snapshot = snapshot
    await env.snapshot(run_deadline=DEADLINE, now=NOW)
    env._usage.settle = settle
    return env


async def test_snapshotted_box_with_an_unstopped_receipt_closes_quietly(caplog: pytest.LogCaptureFixture) -> None:
    """Check that the expected pending receipt of a snapshot builder raises nothing and logs no warning.

    The snapshot call stops the builder session, but Vercel's receipt can
    still show it running for a short time after the stop. The reconciler
    settles the hold later.
    """
    env = await _snapshotted_env_whose_settle_raises([], SessionNotStoppedError("The receipt is not stopped."))
    with caplog.at_level(logging.INFO):
        await env.stop()
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and "settles after its receipt is final" in record.getMessage()
    ]


async def test_snapshotted_box_still_raises_a_usage_anomaly(caplog: pytest.LogCaptureFixture) -> None:
    """Check that a pending usage anomaly of a snapshot builder, not a late receipt, still warns and raises."""
    anomaly = UsagePendingError("Cloud sandbox transfer exceeded the admitted network allowance.")
    env = await _snapshotted_env_whose_settle_raises([], anomaly)
    with pytest.raises(UsagePendingError) as raised:
        await env.stop()
    assert raised.value is anomaly
    assert [record for record in caplog.records if record.levelno == logging.WARNING]


async def test_snapshotted_box_still_raises_a_budget_error(caplog: pytest.LogCaptureFixture) -> None:
    """Check that a budget settle failure of a snapshot builder still warns and raises unchanged."""
    error = BudgetError("The budget is closed.")
    env = await _snapshotted_env_whose_settle_raises([], error)
    with pytest.raises(BudgetError) as raised:
        await env.stop()
    assert raised.value is error
    assert [record for record in caplog.records if record.levelno == logging.WARNING]


async def test_snapshotted_box_wraps_an_unexpected_settle_error(caplog: pytest.LogCaptureFixture) -> None:
    """Check that an unexpected settle failure of a snapshot builder warns and raises a pending usage error."""
    error = RuntimeError("database down")
    env = await _snapshotted_env_whose_settle_raises([], error)
    with pytest.raises(UsagePendingError) as raised:
        await env.stop()
    assert raised.value.__cause__ is error
    assert [record for record in caplog.records if record.levelno == logging.WARNING]


async def test_provider_refuses_to_snapshot_a_box_with_env() -> None:
    """Check that a spec with env values makes no hold and no snapshot call.

    The Vercel provider already refuses injected headers when it is built.
    """
    log: list[str] = []
    env = _snapshotting_env(log, FakeClient(log), env={"API_KEY": "fixture"})
    with pytest.raises(SnapshotRefusedError):
        await env.snapshot(run_deadline=DEADLINE, now=NOW)
    assert "snapshot-reserve" not in log


async def test_provider_refuses_after_a_command_ran_with_env() -> None:
    """Check that a command with env values blocks a later snapshot."""
    log: list[str] = []
    client = FakeClient(log)
    env = _snapshotting_env(log, client)
    await env.start()
    await env.exec("echo hi", timeout_sec=5, env={"TOKEN": "fixture"})
    with pytest.raises(SnapshotRefusedError):
        await env.snapshot(run_deadline=DEADLINE, now=NOW)
    assert "snapshot-reserve" not in log
    await env.stop()


async def test_failed_snapshot_keeps_the_hold_pending_and_is_not_retried() -> None:
    """Check that an uncertain snapshot call is sent once and its hold stays."""
    log: list[str] = []
    client = FakeClient(log)
    env = _snapshotting_env(log, client)
    await env.start()
    calls: list[Any] = []

    async def snapshot(*, expiration: Any = None) -> Any:
        """Fail like a lost response.

        Args:
            expiration: Unused.

        Raises:
            httpx.ReadError: Always.
        """
        calls.append(expiration)
        raise httpx.ReadError("lost")

    client.boxes[env.name].current_session.snapshot = snapshot
    with pytest.raises(httpx.ReadError):
        await env.snapshot(run_deadline=DEADLINE, now=NOW)
    assert len(calls) == 1
    assert "snapshot-pending" in log
    await env.stop()


async def test_trial_box_starts_from_the_snapshot_and_waits_through_image_not_ready() -> None:
    """Check the snapshot source, and that image_not_ready is no failed attempt."""
    log: list[str] = []
    client = FakeClient(log)
    client.create_errors = [_api_error("image_not_ready"), _api_error("image_not_ready")]
    env = make_env(client, log)
    env.snapshot_id = "snap-1"
    await env.start()
    assert log.count("reserve") == 1
    assert "fail" not in log
    assert len(client.create_kwargs) == 3
    for call in client.create_kwargs:
        assert call["image"] is None
        assert call["source"] == SnapshotSource(snapshot_id="snap-1")
    assert env._usage_request()["snapshot_id"] == "snap-1"
    await env.stop()


async def test_other_create_error_from_the_snapshot_is_not_retried() -> None:
    """Check that a real create failure is sent once."""
    log: list[str] = []
    client = FakeClient(log)
    client.create_errors = [_api_error("snapshot_not_found")]
    env = make_env(client, log)
    env.snapshot_id = "snap-1"
    with pytest.raises((SandboxApiError, SandboxInfraError)):
        await env.start()
    assert len(client.create_kwargs) == 1


class FakeSnapshotHandle:
    """Stand in for an SDK snapshot handle."""

    def __init__(self, snapshot_id: str, size_bytes: int) -> None:
        """Make a created snapshot.

        Args:
            snapshot_id: The snapshot id.
            size_bytes: The stored size.
        """
        self.id = snapshot_id
        self.status = "created"
        self.size_bytes = size_bytes
        self.created_at = 1_000
        self.updated_at = 1_000
        self.expires_at = 1_000 + 3 * 86_400_000

    async def delete(self) -> None:
        """Delete the snapshot one day after it was made."""
        self.status = "deleted"
        self.updated_at = self.created_at + 86_400_000


class SnapshotClient(FakeClient):
    """Add snapshot reads to the fake SDK client."""

    def __init__(self, log: list[str], handle: FakeSnapshotHandle) -> None:
        """Keep one snapshot handle.

        Args:
            log: The shared call log.
            handle: The snapshot handle.
        """
        super().__init__(log)
        self.handle = handle

    async def get_snapshot(self, *, snapshot_id: str) -> FakeSnapshotHandle:
        """Return the snapshot handle.

        Args:
            snapshot_id: The snapshot id.

        Returns:
            The handle.
        """
        assert snapshot_id == self.handle.id
        return self.handle


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Create a private funded wallet and ledger with the markup pinned to 1.0.

    Args:
        tmp_path: The test directory.
        monkeypatch: Pins the usage markup.

    Yields:
        The SQLite engine.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshots.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


async def test_failed_bind_deletes_the_snapshot_and_removes_the_builder() -> None:
    """Check that a snapshot whose hold was not bound is deleted, settled and not kept."""
    log: list[str] = []
    handle = FakeSnapshotHandle("snap-9", 1_000)
    client = SnapshotClient(log, handle)
    env = _snapshotting_env(log, client)

    def factory(budget: Any, request: dict[str, Any], *, operation_key: str) -> FailingBindUsage:
        """Make a reservation whose bind fails.

        Args:
            budget: Unused.
            request: The snapshot request.
            operation_key: The operation key.

        Returns:
            The fake reservation.
        """
        return FailingBindUsage(log, request, operation_key)

    env._snapshot_usage_factory = factory
    await env.start()

    async def snapshot(*, expiration: Any = None) -> Any:
        """Return a created snapshot.

        Args:
            expiration: Unused.

        Returns:
            The snapshot handle.
        """
        return SimpleNamespace(id="snap-9", status="created", expires_at=1_000)

    client.boxes[env.name].current_session.snapshot = snapshot
    with pytest.raises(RuntimeError):
        await env.snapshot(run_deadline=DEADLINE, now=NOW)
    assert handle.status == "deleted"
    assert "snapshot-settle:deleted" in log
    assert env._snapshotted is False
    await env.stop()
    assert client.boxes[env.name].destroys >= 1


def _runtime(database: Engine) -> BudgetRuntime:
    """Bind real admission to the local database.

    Args:
        database: The SQLite engine.

    Returns:
        The budget runtime.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def _operation(database: Engine) -> ExecutionOperationModel:
    """Read the one snapshot operation.

    Args:
        database: The SQLite engine.

    Returns:
        The operation row.
    """
    with Session(database) as session:
        return session.scalars(select(ExecutionOperationModel)).one()


async def test_deleted_snapshot_is_metered_as_sandbox_cost(database: Engine) -> None:
    """Check that the run-end delete settles the storage as sandbox cost and removes the builder."""
    runtime = _runtime(database)
    reservation = VercelSnapshotReservation(
        runtime, {"session_id": "session-1", "retention_ms": 3 * 86_400_000}, operation_key="op-1:snapshot"
    )
    reservation.bind("snap-1")
    log: list[str] = []
    client = SnapshotClient(log, FakeSnapshotHandle("snap-1", 10_000_000_000))
    builder = client.boxes["builder"] = FakeBox("builder", FakeSession())
    built = BuiltSnapshot(snapshot_id="snap-1", builder_name="builder", reservation=reservation)
    await VercelEnvironment.delete_snapshot(built, client=client)
    operation = _operation(database)
    assert operation.state == "settled"
    assert operation.cost_kind == "sandbox"
    assert operation.actual_units > 0
    assert builder.destroys == 1
    assert client.closed == 0
    budget = runtime.service.get(runtime.budget_id, "alice")
    assert budget.reserved_cents == 0
    assert budget.billed_cents > 0


class VercelLikeSnapshotHandle(FakeSnapshotHandle):
    """Act like Vercel: the delete leaves ``updated_at`` at the creation time."""

    def __init__(self, snapshot_id: str, size_bytes: int) -> None:
        """Make a snapshot created one day ago.

        Args:
            snapshot_id: The snapshot id.
            size_bytes: The stored size.
        """
        super().__init__(snapshot_id, size_bytes)
        self.created_at = self.updated_at = int(time.time() * 1000) - 86_400_000
        self.expires_at = self.created_at + 3 * 86_400_000

    async def delete(self) -> None:
        """Delete the snapshot without a change to its timestamps."""
        self.status = "deleted"


def _storage_units(reservation: VercelSnapshotReservation, size_bytes: int, stored_ms: int) -> int:
    """Price stored snapshot bytes for a stored time at the admitted quote.

    Args:
        reservation: The snapshot hold with its admitted quote.
        size_bytes: The stored size.
        stored_ms: The stored time.

    Returns:
        The charge in budget units.
    """
    price = reservation.quote.price_snapshot
    usd = vercel_snapshot_actual_usd(
        {"id": "snap-1", "status": "deleted", "size_bytes": size_bytes, "created_at": 0, "updated_at": stored_ms},
        snapshot_id="snap-1",
        price_snapshot=price,
    )
    return cent_units(vercel_charge_policy(price).convert(usd).total)


async def test_snapshot_storage_is_priced_to_our_delete_time(database: Engine) -> None:
    """Check that a snapshot is billed for its stored day when Vercel keeps ``updated_at`` at creation."""
    runtime = _runtime(database)
    reservation = VercelSnapshotReservation(
        runtime, {"session_id": "session-1", "retention_ms": 3 * 86_400_000}, operation_key="op-1:snapshot"
    )
    reservation.bind("snap-1")
    client = SnapshotClient([], VercelLikeSnapshotHandle("snap-1", 10_000_000_000))
    client.boxes["builder"] = FakeBox("builder", FakeSession())
    built = BuiltSnapshot(snapshot_id="snap-1", builder_name="builder", reservation=reservation)
    await VercelEnvironment.delete_snapshot(built, client=client)
    operation = _operation(database)
    assert operation.state == "settled"
    one_day = _storage_units(reservation, 10_000_000_000, 86_400_000)
    assert one_day > 0
    assert one_day <= operation.actual_units <= _storage_units(reservation, 10_000_000_000, 86_460_000)
    assert runtime.service.get(runtime.budget_id, "alice").billed_cents > 0


class FlakySettleReservation(VercelSnapshotReservation):
    """Fail the first settle after the delete reached Vercel, then settle normally."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Make the hold with no settle call yet.

        Args:
            *args: The ``VercelSnapshotReservation`` arguments.
            **kwargs: The ``VercelSnapshotReservation`` keyword arguments.
        """
        super().__init__(*args, **kwargs)
        self.settle_calls = 0

    def settle(self, snapshot: Any) -> None:
        """Raise like a database outage on the first call, then settle.

        Args:
            snapshot: The provider state of the snapshot.

        Raises:
            RuntimeError: On the first call.
        """
        self.settle_calls += 1
        if self.settle_calls == 1:
            self.pending(snapshot)
            raise RuntimeError("database down")
        super().settle(snapshot)


async def test_snapshot_delete_retry_prices_storage_to_the_retry_time(database: Engine) -> None:
    """Check that a retry after a delete that reached Vercel still bills the stored day.

    The first settle fails after the delete, so the retry finds the snapshot
    already deleted with ``updated_at`` at the creation time.
    """
    runtime = _runtime(database)
    reservation = FlakySettleReservation(
        runtime, {"session_id": "session-1", "retention_ms": 3 * 86_400_000}, operation_key="op-1:snapshot"
    )
    reservation.bind("snap-1")
    client = SnapshotClient([], VercelLikeSnapshotHandle("snap-1", 10_000_000_000))
    client.boxes["builder"] = FakeBox("builder", FakeSession())
    built = BuiltSnapshot(snapshot_id="snap-1", builder_name="builder", reservation=reservation)
    with pytest.raises(RuntimeError):
        await VercelEnvironment.delete_snapshot(built, client=client)
    await VercelEnvironment.delete_snapshot(built, client=client)
    operation = _operation(database)
    assert reservation.settle_calls == 2
    assert operation.state == "settled"
    one_day = _storage_units(reservation, 10_000_000_000, 86_400_000)
    assert one_day > 0
    assert one_day <= operation.actual_units <= _storage_units(reservation, 10_000_000_000, 86_460_000)


async def test_missing_builder_record_is_normal_after_the_delete(
    database: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that a 404 for the builder still settles the snapshot and logs no warning."""
    runtime = _runtime(database)
    reservation = VercelSnapshotReservation(
        runtime, {"session_id": "session-1", "retention_ms": 3 * 86_400_000}, operation_key="op-1:snapshot"
    )
    reservation.bind("snap-1")
    log: list[str] = []
    handle = FakeSnapshotHandle("snap-1", 10_000_000_000)
    client = SnapshotClient(log, handle)

    async def get_sandbox(*, name: str) -> Any:
        """Answer like Vercel for a sandbox with no record.

        Args:
            name: Unused.

        Raises:
            SandboxApiError: Always, with a 404 answer.
        """
        raise SandboxApiError(httpx.Response(404), "not found", data={"error": {"code": "not_found"}})

    client.get_sandbox = get_sandbox
    built = BuiltSnapshot(snapshot_id="snap-1", builder_name="builder", reservation=reservation)
    with caplog.at_level(logging.INFO, logger="core.sandbox.providers.vercel"):
        await VercelEnvironment.delete_snapshot(built, client=client)
    assert handle.status == "deleted"
    assert _operation(database).state == "settled"
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert any("no record" in record.getMessage() for record in caplog.records)


def test_undeleted_snapshot_stays_pending(database: Engine) -> None:
    """Check that a snapshot that still exists keeps its hold."""
    runtime = _runtime(database)
    reservation = VercelSnapshotReservation(
        runtime, {"session_id": "session-1", "retention_ms": 86_400_000}, operation_key="op-1:snapshot"
    )
    reservation.bind("snap-1")
    handle = FakeSnapshotHandle("snap-1", 1_000)
    with pytest.raises(UsagePendingError):
        reservation.settle(vars(handle))
    assert _operation(database).state == "pending"


def test_snapshot_without_a_bounded_expiry_is_not_reserved(database: Engine) -> None:
    """Check that an unbounded expiry makes no hold."""
    runtime = _runtime(database)
    with pytest.raises(UnpricedOperationError):
        VercelSnapshotReservation(runtime, {"session_id": "session-1", "retention_ms": 0}, operation_key="op-1:snap")
    with Session(database) as session:
        assert session.scalars(select(ExecutionOperationModel)).first() is None


class FastClock:
    """Poll the lock quickly with real time."""

    def monotonic(self) -> float:
        """Return the real monotonic time.

        Returns:
            Seconds.
        """
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Wait a short real time.

        Args:
            seconds: Unused.
        """
        await asyncio.sleep(0.02)


@pytest.mark.skipif(
    not TEST_DB_URL or not TEST_DB_URL.startswith("postgresql"),
    reason="Set APP_BUDGET_TEST_DB_URL to an explicitly disposable PostgreSQL database.",
)
async def test_two_pods_build_one_snapshot_under_the_advisory_lock() -> None:
    """Check that two caches on one database build a key once."""
    engine = create_engine(TEST_DB_URL)
    provider = Provider()
    try:
        pods = [
            SnapshotCache(
                "run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup, engine=engine, clock=FastClock()
            )
            for _ in range(2)
        ]
        results = await asyncio.gather(*(pod.get_or_build(_key(), provider.build) for pod in pods))
        assert len(provider.builds) == 1
        assert {built.snapshot_id for built in results} == {"snap-1"}
    finally:
        engine.dispose()


async def test_release_deletes_the_key_once_and_close_skips_it() -> None:
    """Check that release deletes only its key's snapshot, a second release is a no-op, and close skips it."""
    provider = Provider()
    cache = SnapshotCache("run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup)
    await cache.get_or_build(_key(), provider.build)
    await cache.get_or_build(_key(install_command="uv sync"), provider.build)
    await cache.release(_key())
    assert provider.deletes == ["snap-1"]
    await cache.release(_key())
    assert provider.deletes == ["snap-1"]
    await cache.close()
    assert provider.deletes == ["snap-1", "snap-2"]


async def test_failed_release_is_retried_by_close_and_deleted_once() -> None:
    """Check that a failed release raises, close deletes it once, and nothing deletes it twice."""
    provider = Provider()
    calls: list[str] = []

    async def flaky_delete(built: BuiltSnapshot) -> None:
        """Fail the first delete, then delete.

        Args:
            built: The snapshot to delete.
        """
        calls.append(built.snapshot_id)
        if len(calls) == 1:
            raise RuntimeError("provider error")
        await provider.delete(built)

    cache = SnapshotCache("run-1", DEADLINE, delete=flaky_delete)
    await cache.get_or_build(_key(), provider.build)
    with pytest.raises(RuntimeError):
        await cache.release(_key())
    await cache.close()
    await cache.close()
    await cache.release(_key())
    assert calls == ["snap-1", "snap-1"]
    assert provider.deletes == ["snap-1"]


async def test_release_of_an_evicted_and_rebuilt_key_deletes_both_snapshots() -> None:
    """Check that release finds the evicted snapshot of its key by its recorded id."""
    provider = Provider()
    clock = FakeClock()
    cache = SnapshotCache("run-1", DEADLINE, delete=provider.delete, clock=clock)
    await cache.get_or_build(_key(), provider.build)
    clock.now = 1_000.0
    assert await cache.evict_idle(600.0) == [_key()]
    await cache.get_or_build(_key(), provider.build)
    await cache.release(_key())
    assert sorted(provider.deletes) == ["snap-1", "snap-2"]


async def test_release_of_a_borrowed_snapshot_only_drops_it() -> None:
    """Check that a release never deletes a snapshot that another pod built."""
    provider = Provider()
    owner = SnapshotCache("run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup)
    borrower = SnapshotCache("run-1", DEADLINE, delete=provider.delete, lookup=provider.lookup)
    await owner.get_or_build(_key(), provider.build)
    await borrower.get_or_build(_key(), provider.build)
    await borrower.release(_key())
    assert provider.deletes == []
    await owner.release(_key())
    assert provider.deletes == ["snap-1"]


async def test_release_deletes_a_snapshot_whose_builder_is_the_provider_box() -> None:
    """Check that release finds a snapshot by its key when the provider names the builder after its own box."""
    deleted: list[tuple[str, object]] = []
    hold = object()

    async def delete(built: BuiltSnapshot) -> None:
        """Record the delete and the hold it settles.

        Args:
            built: The snapshot to delete.
        """
        deleted.append((built.snapshot_id, built.reservation))

    async def build(_name: str) -> BuiltSnapshot:
        """Return a snapshot named after the provider box, as the Vercel provider does.

        Args:
            _name: The cache's builder name, which the provider ignores.

        Returns:
            The snapshot.
        """
        return BuiltSnapshot(snapshot_id="snap-1", builder_name="skynet-0123456789abcdef", reservation=hold)

    cache = SnapshotCache("run-1", DEADLINE, delete=delete)
    await cache.get_or_build(_key(), build)
    await cache.release(_key())
    assert deleted == [("snap-1", hold)]
    await cache.close()
    assert deleted == [("snap-1", hold)]


async def test_close_waits_for_a_release_delete_that_fails_and_deletes_it() -> None:
    """Check that a close during a release's failing delete still deletes the snapshot."""
    provider = Provider()
    calls: list[str] = []
    started = asyncio.Event()
    go_on = asyncio.Event()

    async def slow_flaky_delete(built: BuiltSnapshot) -> None:
        """Wait, then fail the first delete; delete on later calls.

        Args:
            built: The snapshot to delete.
        """
        calls.append(built.snapshot_id)
        if len(calls) == 1:
            started.set()
            await go_on.wait()
            raise RuntimeError("provider error")
        await provider.delete(built)

    cache = SnapshotCache("run-1", DEADLINE, delete=slow_flaky_delete)
    await cache.get_or_build(_key(), provider.build)
    release = asyncio.create_task(cache.release(_key()))
    await started.wait()
    close = asyncio.create_task(cache.close())
    await asyncio.sleep(0)
    assert not close.done()
    go_on.set()
    with pytest.raises(RuntimeError):
        await release
    await close
    assert calls == ["snap-1", "snap-1"]
    assert provider.deletes == ["snap-1"]


async def test_cancelled_delete_is_kept_for_the_next_close() -> None:
    """Check that a close cancelled during a delete keeps the snapshot for the next close."""
    provider = Provider()
    calls: list[str] = []
    started = asyncio.Event()

    async def hanging_delete(built: BuiltSnapshot) -> None:
        """Hang on the first delete; delete on later calls.

        Args:
            built: The snapshot to delete.
        """
        calls.append(built.snapshot_id)
        if len(calls) == 1:
            started.set()
            await asyncio.Event().wait()
        await provider.delete(built)

    cache = SnapshotCache("run-1", DEADLINE, delete=hanging_delete)
    await cache.get_or_build(_key(), provider.build)
    close = asyncio.create_task(cache.close())
    await started.wait()
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    await cache.close()
    assert provider.deletes == ["snap-1"]

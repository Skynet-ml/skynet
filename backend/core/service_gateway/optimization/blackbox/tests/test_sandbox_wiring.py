"""Tests that compose services and snapshot ids reach the provider through the bridge and the broker (SKY-531)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest

from core.exceptions import ServiceError
from core.sandbox import factory as sandbox_factory
from core.sandbox.capabilities import EnvironmentCapabilities
from core.sandbox.environment import SandboxSpec as AsyncSandboxSpec
from core.sandbox.errors import SandboxFatalError
from core.sandbox.factory import ProviderEntry
from core.sandbox.services import ComposeService
from core.sandbox.snapshots import BuiltSnapshot, SnapshotCache
from core.sandbox.sync_bridge import run as bridge_run
from core.sandbox.tests.fake_provider import FAKE_CAPABILITIES, InMemorySandbox

from ..sandbox import AsyncBridgeRuntime, SandboxSpec
from ..sandbox_broker import SandboxBroker
from .test_sandbox_broker import IMAGE, FakeRuntime, FakeSession

_DB = ComposeService(name="db", command="postgres -D /data")


class ServiceSandbox(InMemorySandbox):
    """Act as a provider that starts compose services and boxes from snapshots."""

    built: ClassVar[list[str]] = []

    def __init__(self, spec: AsyncSandboxSpec, *, snapshot_id: str | None = None, services: Any = ()) -> None:
        """Record the extra constructor arguments.

        Args:
            spec: The sandbox to open.
            snapshot_id: The snapshot the box starts from.
            services: The compose services the box starts.
        """
        super().__init__(spec)
        self.snapshot_id = snapshot_id
        self.services = tuple(services)

    async def snapshot(self, *, run_deadline: datetime) -> BuiltSnapshot:
        """Save the box once and name the snapshot after the call count.

        Args:
            run_deadline: The run's end, which bounds the snapshot's life.

        Returns:
            The built snapshot.
        """
        type(self).built.append(run_deadline.isoformat())
        return BuiltSnapshot(f"snap-{len(type(self).built)}", "skynet-box-0123456789")


class PlainSandbox(InMemorySandbox):
    """Act as a provider with no snapshots and no compose services."""

    capabilities: ClassVar[EnvironmentCapabilities] = replace(FAKE_CAPABILITIES, snapshots=False)


def _runtime(monkeypatch: pytest.MonkeyPatch, cls: str) -> AsyncBridgeRuntime:
    """Build a bridge runtime over one of this module's fake providers.

    Args:
        monkeypatch: Pytest fixture.
        cls: The fake provider class name.

    Returns:
        The runtime.
    """
    monkeypatch.setitem(sandbox_factory.PROVIDERS, "vercel", ProviderEntry(__name__, cls, None, True))
    return AsyncBridgeRuntime("vercel")


def _cache(deleted: list[str]) -> SnapshotCache:
    """Build a run cache that records each deleted snapshot.

    Args:
        deleted: Gets the deleted snapshot ids.

    Returns:
        The cache.
    """

    async def delete(built: BuiltSnapshot) -> None:
        """Record the delete.

        Args:
            built: The snapshot to delete.
        """
        deleted.append(built.snapshot_id)

    return SnapshotCache("run-1", datetime.now(UTC) + timedelta(hours=1), delete=delete)


def test_the_bridge_passes_services_and_the_snapshot_to_a_capable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand the compose services and the snapshot id to a provider that takes them."""
    runtime = _runtime(monkeypatch, "ServiceSandbox")
    assert runtime.supports_snapshots
    session = runtime.open(SandboxSpec(lifetime_seconds=60, services=(_DB,), snapshot_id="snap-9"))
    try:
        environment = session._session.environment
        assert environment.services == (_DB,)
        assert environment.snapshot_id == "snap-9"
    finally:
        session.close()


def test_the_bridge_drops_the_snapshot_and_refuses_services_without_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open an empty box for a snapshot id, and refuse compose services, on a provider without them."""
    runtime = _runtime(monkeypatch, "PlainSandbox")
    assert not runtime.supports_snapshots
    session = runtime.open(SandboxSpec(lifetime_seconds=60, snapshot_id="snap-9"))
    session.close()
    with pytest.raises(ServiceError, match="cannot run the repository's compose services"):
        runtime.open(SandboxSpec(lifetime_seconds=60, services=(_DB,)))


def test_a_bridge_snapshot_is_built_once_per_key_and_deleted_with_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reuse the cached snapshot for the same key, and delete it when the run's cache closes."""
    ServiceSandbox.built.clear()
    runtime = _runtime(monkeypatch, "ServiceSandbox")
    deleted: list[str] = []
    cache = _cache(deleted)
    session = runtime.open(SandboxSpec(lifetime_seconds=60))
    try:
        assert session.snapshot(cache, "key-a") == "snap-1"
        assert session.snapshot(cache, "key-a") == "snap-1"
        assert len(ServiceSandbox.built) == 1
    finally:
        session.close()
    bridge_run(cache.close)
    assert deleted == ["snap-1"]


class _SnapshotSession(FakeSession):
    """Record the snapshots a parent box takes."""

    def __init__(self, order: list[str]) -> None:
        """Start with no snapshots.

        Args:
            order: Gets the close and snapshot events in order.
        """
        super().__init__()
        self.order = order
        self.taken: list[tuple[SnapshotCache, str]] = []

    def snapshot(self, cache: SnapshotCache, cache_key: str) -> str:
        """Save this box in the cache.

        Args:
            cache: The run's cache.
            cache_key: Names the setup.

        Returns:
            A fake snapshot id.
        """
        self.taken.append((cache, cache_key))

        async def build(_builder: str) -> BuiltSnapshot:
            """Return a fixed snapshot.

            Args:
                _builder: Unused builder name; a provider names the builder after its own box.

            Returns:
                The snapshot.
            """
            return BuiltSnapshot("snap-p", "skynet-box-0123456789")

        return bridge_run(cache.get_or_build, cache_key, build).snapshot_id

    def close(self) -> None:
        """Record the close."""
        self.order.append("close")
        super().close()


class _SnapshotRuntime(FakeRuntime):
    """Open parent boxes that can take snapshots."""

    def __init__(self) -> None:
        """Start with no boxes."""
        super().__init__()
        self.order: list[str] = []

    def open(self, spec: SandboxSpec) -> _SnapshotSession:
        """Return a recording box.

        Args:
            spec: The requested box.

        Returns:
            The box.
        """
        self.specs.append(spec)
        session = _SnapshotSession(self.order)
        self.sessions.append(session)
        return session


def test_a_parent_box_keeps_its_services_and_snapshot_id() -> None:
    """Keep the compose services and the snapshot id when the broker rebuilds a parent box's spec."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120, snapshots=_cache([]))
    broker.parent_runtime(()).open(
        SandboxSpec(lifetime_seconds=60, operation_key="k", services=(_DB,), snapshot_id="snap-3")
    )
    (spec,) = runtime.specs
    assert spec.services == (_DB,)
    assert spec.snapshot_id == "snap-3"
    assert spec.image == IMAGE


def test_a_parent_box_without_the_runs_cache_drops_its_snapshot_id() -> None:
    """Open an empty box when the run has no snapshot cache, so no snapshot runs outside a storage hold."""
    runtime = FakeRuntime()
    parent = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120).parent_runtime(())
    parent.open(SandboxSpec(lifetime_seconds=60, operation_key="k", services=(_DB,), snapshot_id="snap-3"))
    (spec,) = runtime.specs
    assert spec.services == (_DB,)
    assert spec.snapshot_id is None


def test_a_parent_snapshot_needs_the_runs_cache() -> None:
    """Return no snapshot when the run has no cache, so the caller keeps its box."""
    runtime = _SnapshotRuntime()
    parent = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120).parent_runtime(())
    box = parent.open(SandboxSpec(lifetime_seconds=60, operation_key="k"))
    assert parent.snapshot(box, "key-a") is None
    assert runtime.sessions[0].taken == []


def test_the_broker_snapshots_in_its_one_cache_and_deletes_after_the_boxes_close() -> None:
    """Take parent snapshots in the run's cache, and delete them only after every box closes."""
    runtime = _SnapshotRuntime()
    cache = _cache(runtime.order)
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120, snapshots=cache)
    parent = broker.parent_runtime(())
    box = parent.open(SandboxSpec(lifetime_seconds=60, operation_key="k"))
    assert parent.snapshot(box, "key-a") == "snap-p"
    assert runtime.sessions[0].taken == [(cache, "key-a")]
    broker.close()
    assert runtime.order == ["close", "snap-p"]
    with pytest.raises(SandboxFatalError, match="run ended"):
        bridge_run(cache.get_or_build, "key-b", None)


def test_the_broker_release_deletes_a_snapshot_named_after_the_provider_box() -> None:
    """Delete a key's snapshot at release when the provider names its builder after the box, not the key."""
    runtime = _SnapshotRuntime()
    cache = _cache(runtime.order)
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120, snapshots=cache)
    parent = broker.parent_runtime(())
    box = parent.open(SandboxSpec(lifetime_seconds=60, operation_key="k"))
    assert parent.snapshot(box, "key-a") == "snap-p"
    parent.release_snapshot("key-a")
    assert runtime.order == ["snap-p"]
    broker.close()
    assert runtime.order == ["snap-p", "close"]

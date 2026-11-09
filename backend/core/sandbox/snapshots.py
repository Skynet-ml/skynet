"""Cache one setup-box snapshot per run, so trial boxes skip the install.

The run builds its setup box once, takes a Vercel snapshot after setup and
before any secret enters the box, and starts each trial box from that
snapshot. The snapshot lives only for the run: the user pays for its storage
as sandbox cost, and the run deletes it at its end. Vercel counts the expiry
from the snapshot's last use, so the storage hold covers a last use at the
run deadline plus that expiry.

Adapted from Harbor 0.25.0 (harbor-framework/harbor), Apache-2.0,
``src/harbor/environments/vercel/snapshots.py``. Changes:

- The cache is scoped to one run, not to a machine, and it deletes every
  snapshot at run end instead of keeping a shared library.
- The key hashes the image digest, the setup and install commands, the lock
  files and a spec version, not a Dockerfile.
- A Postgres advisory lock lets only one pod build a key; Harbor uses a
  local file lock.
- A box that holds env values or injected headers is never snapshotted.
- A failed build is never retried and nothing is cached for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .errors import SandboxFatalError, SandboxInfraError
from .pacing import SYSTEM_CLOCK, Clock
from .resources import SandboxSpec

logger = logging.getLogger(__name__)

# Bump when the build steps change in a way the other key inputs do not show.
SNAPSHOT_SPEC_VERSION = "skynet-setup-snapshot-v1"
INSTALL_TIMEOUT = 900.0
SNAPSHOT_TIMEOUT = 600.0
EXPIRY_GRACE = timedelta(days=1)
# Vercel refuses a snapshot expiry below one day.
MINIMUM_EXPIRY = timedelta(days=1)
LOCK_POLL = 2.0
# Another pod may hold the lock for a full install plus a snapshot.
LOCK_WAIT = INSTALL_TIMEOUT + SNAPSHOT_TIMEOUT + 60.0


class SnapshotRefusedError(SandboxFatalError):
    """Report a box that must never become a snapshot."""


class SnapshotLockTimeoutError(SandboxInfraError):
    """Report that another pod held the build lock for too long."""


def _digest(value: str | bytes) -> str:
    """Hash text or bytes with SHA-256.

    Args:
        value: The content to hash.

    Returns:
        The hex digest.
    """
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def snapshot_cache_key(
    *,
    image_digest: str,
    setup_command: str,
    install_command: str,
    lock_files: Mapping[str, str | bytes],
    spec_version: str = SNAPSHOT_SPEC_VERSION,
) -> str:
    """Return the cache key of a setup box.

    Args:
        image_digest: The immutable base image reference.
        setup_command: The command that prepares the repository.
        install_command: The command that installs the dependencies.
        lock_files: Each lock file path mapped to its content.
        spec_version: The version of the build steps.

    Returns:
        A hex SHA-256 that changes when any input changes.
    """
    payload = {
        "image_digest": image_digest,
        "setup_command": setup_command,
        "install_command": install_command,
        "lock_files": [[path, _digest(content)] for path, content in sorted(lock_files.items())],
        "spec_version": spec_version,
    }
    return _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _scoped(run_id: str, cache_key: str) -> bytes:
    """Hash a cache key inside one run.

    Args:
        run_id: The run that owns the snapshot.
        cache_key: The setup cache key.

    Returns:
        The SHA-256 digest bytes.
    """
    return hashlib.sha256(f"{run_id}\0{cache_key}".encode()).digest()


def advisory_lock_key(run_id: str, cache_key: str) -> int:
    """Return the Postgres advisory lock key of one run-scoped cache key.

    Args:
        run_id: The run that owns the snapshot.
        cache_key: The setup cache key.

    Returns:
        A signed 64-bit integer, the type that ``pg_try_advisory_xact_lock`` takes.
    """
    return int.from_bytes(_scoped(run_id, cache_key)[:8], "big", signed=True)


def snapshot_box_name(run_id: str, cache_key: str) -> str:
    """Return the stable name of the builder box of one cache key.

    Every pod derives the same name, so a pod can find a snapshot that another
    pod built.

    Args:
        run_id: The run that owns the snapshot.
        cache_key: The setup cache key.

    Returns:
        A Vercel sandbox name.
    """
    return "skynet-snap-" + _scoped(run_id, cache_key).hex()[:40]


def refuse_secrets(spec: SandboxSpec) -> None:
    """Refuse a box spec that carries secrets.

    A snapshot keeps the whole file system and every later trial box starts
    from it, so a value in it would leak into every trial. This guard does not
    cover files sent with ``upload_files`` or written by setup commands.

    Args:
        spec: The spec of the box to snapshot.

    Raises:
        SnapshotRefusedError: The spec has env values or injected headers.
    """
    if spec.env:
        raise SnapshotRefusedError("A box with env values cannot become a snapshot.")
    if spec.inject_headers:
        raise SnapshotRefusedError("A box with injected headers cannot become a snapshot.")


def snapshot_expiration(run_deadline: datetime, now: datetime) -> timedelta:
    """Return the Vercel expiry of a run snapshot.

    Vercel counts this expiry from the snapshot's last use, so it is not the
    full storage time; see :func:`snapshot_hold_retention`.

    Args:
        run_deadline: The latest time the run can end.
        now: The current time.

    Returns:
        The time from now to the deadline plus one day, at least one day.
    """
    return max(run_deadline + EXPIRY_GRACE - now, MINIMUM_EXPIRY)


def snapshot_hold_retention(run_deadline: datetime, now: datetime) -> timedelta:
    """Return the longest time a run snapshot can be stored.

    Vercel docs say the expiry counts from the snapshot's last use, not from
    its creation. A trial box can use the snapshot up to the run deadline, so
    a snapshot that the run fails to delete can live until the deadline plus
    the expiry.

    Args:
        run_deadline: The latest time the run can end.
        now: The current time.

    Returns:
        The time from now to the deadline, never below zero, plus the expiry
        from :func:`snapshot_expiration`.
    """
    return max(run_deadline - now, timedelta(0)) + snapshot_expiration(run_deadline, now)


@dataclass(frozen=True)
class BuiltSnapshot:
    """Describe one snapshot that trial boxes can start from.

    ``reservation`` is the budget hold for its storage, or ``None`` for an
    unprotected box or a snapshot that another pod built.
    """

    snapshot_id: str
    builder_name: str
    expires_at_ms: int | None = None
    reservation: Any = None


Lookup = Callable[[str], Awaitable[BuiltSnapshot | None]]
Delete = Callable[[BuiltSnapshot], Awaitable[None]]
Build = Callable[[str], Awaitable[BuiltSnapshot]]


def _utcnow() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class SnapshotCache:
    """Build each setup snapshot of one run once and delete them at run end."""

    def __init__(
        self,
        run_id: str,
        run_deadline: datetime,
        *,
        delete: Delete,
        lookup: Lookup | None = None,
        engine: Engine | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Create an empty cache for one run.

        Args:
            run_id: The run that owns the snapshots.
            run_deadline: The latest time the run can end.
            delete: Deletes one snapshot and settles its hold.
            lookup: Finds a snapshot that another pod built, by builder name.
            engine: The database for the cross-pod build lock. Without a
                Postgres engine, only this process is locked.
            clock: The time source for lock polling and idle tracking.
        """
        self.run_id = run_id
        self.run_deadline = run_deadline
        self._delete = delete
        self._lookup = lookup
        self._engine = engine
        self._clock = clock or SYSTEM_CLOCK
        self._entries: dict[str, BuiltSnapshot] = {}
        self._last_used: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Snapshots this pod built, by snapshot id. Only ``close`` deletes them,
        # because pods that borrowed one cannot tell the owner they still use it.
        self._owned: dict[str, BuiltSnapshot] = {}
        self._closed = False

    def expiration(self, now: datetime | None = None) -> timedelta:
        """Return the Vercel expiry for a snapshot built now.

        Args:
            now: The current time, or ``None`` for the wall clock.

        Returns:
            The expiry from :func:`snapshot_expiration`.
        """
        return snapshot_expiration(self.run_deadline, now or _utcnow())

    def builder_name(self, cache_key: str) -> str:
        """Return the builder box name of a cache key in this run.

        Args:
            cache_key: The setup cache key.

        Returns:
            The name from :func:`snapshot_box_name`.
        """
        return snapshot_box_name(self.run_id, cache_key)

    async def get_or_build(self, cache_key: str, build: Build) -> BuiltSnapshot:
        """Return the snapshot of a key, and build it only when no pod has it.

        A build failure is raised as is, once: the cache never retries it and
        keeps nothing for the key.

        Args:
            cache_key: The setup cache key.
            build: Builds the snapshot; it gets the builder box name.

        Returns:
            The snapshot for trial boxes.

        Raises:
            SandboxFatalError: The run already ended, or it ended during the
                build; that late snapshot is deleted first.
            SnapshotLockTimeoutError: Another pod held the lock too long.
        """
        self._require_open()
        hit = self._touch(cache_key)
        if hit is not None:
            return hit
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            hit = self._touch(cache_key)
            if hit is not None:
                return hit
            async with self._cross_pod_lock(cache_key):
                self._require_open()
                name = self.builder_name(cache_key)
                found = await self._lookup(name) if self._lookup is not None else None
                built = found if found is not None else await build(name)
                if self._closed:
                    if found is None:
                        await self._discard_late_build(built)
                    self._require_open()
                if found is None:
                    self._owned[built.snapshot_id] = built
                self._entries[cache_key] = built
                self._last_used[cache_key] = self._clock.monotonic()
                return built

    async def evict_idle(self, idle_for: float) -> list[str]:
        """Drop the cache entries that no trial on this pod used for a time.

        Eviction never deletes at the provider, for owned and borrowed
        snapshots alike: this pod's idle time says nothing about trials on
        other pods. The snapshot stays live until :meth:`close` or its Vercel
        expiry, and its storage hold stays open until then. The next
        :meth:`get_or_build` of a dropped key looks the snapshot up again, or
        builds a new one when no lookup is set.

        Args:
            idle_for: The idle time, in seconds, after which an entry goes.

        Returns:
            The cache keys that left the cache.
        """
        cutoff = self._clock.monotonic() - idle_for
        dropped: list[str] = []
        for key in [key for key, used in self._last_used.items() if used <= cutoff]:
            async with self._locks.setdefault(key, asyncio.Lock()):
                used = self._last_used.get(key)
                if key in self._entries and used is not None and used <= cutoff:
                    del self._entries[key]
                    del self._last_used[key]
                    dropped.append(key)
        return dropped

    async def release(self, cache_key: str) -> None:
        """Drop one key and delete the snapshots this pod built for it.

        Call it when the only user of a key ends, so its storage hold settles
        then and not at run end. Each delete settles its hold once. A failed
        delete stays owned, so ``close`` tries it again; the first failure is
        raised after every delete. A second call finds nothing to delete. A
        snapshot borrowed from another pod is only dropped from the cache.

        Args:
            cache_key: The key whose snapshots end.

        Raises:
            Exception: The first failed delete, after all of them ran.
        """
        name = self.builder_name(cache_key)
        async with self._locks.setdefault(cache_key, asyncio.Lock()):
            self._entries.pop(cache_key, None)
            self._last_used.pop(cache_key, None)
            failure: Exception | None = None
            # By builder name, not by entry: an evicted and rebuilt key owns
            # more than one snapshot, and the evicted one has no entry.
            for snapshot_id in [key for key, built in self._owned.items() if built.builder_name == name]:
                error = await self._delete_owned(snapshot_id)
                failure = failure or error
        if failure is not None:
            raise failure

    async def close(self) -> None:
        """Delete every snapshot that this pod built and refuse new builds.

        Call it only when the whole run ends on every pod: a borrower pod
        loses its snapshot here. Each delete settles its storage hold. A build
        that is still running when the cache closes deletes its own snapshot
        when it ends. Each delete runs even when an earlier one fails; a
        failed one stays for the next close, and the first failure is raised
        after all of them.
        """
        self._closed = True
        self._entries.clear()
        self._last_used.clear()
        failure: BaseException | None = None
        for snapshot_id in list(self._owned):
            error = await self._delete_owned(snapshot_id)
            failure = failure or error
        if failure is not None:
            raise failure

    async def _delete_owned(self, snapshot_id: str) -> Exception | None:
        """Delete one owned snapshot, or put it back for a later try when the delete fails.

        It leaves ``_owned`` before the delete starts, so a ``release`` and a
        ``close`` that overlap never delete it, and settle its hold, twice.

        Args:
            snapshot_id: The owned snapshot.

        Returns:
            The delete's error, or ``None`` when it was deleted or already gone.
        """
        built = self._owned.pop(snapshot_id, None)
        if built is None:
            return None
        try:
            await self._delete(built)
        except Exception as error:
            logger.warning("Run %s could not delete a setup snapshot: %s", self.run_id, error)
            self._owned[snapshot_id] = built
            return error
        return None

    async def _discard_late_build(self, built: BuiltSnapshot) -> None:
        """Delete a snapshot whose build ended after the run ended.

        Args:
            built: The new snapshot, which no trial may use.

        Raises:
            SandboxFatalError: The delete failed; the hold stays pending until
                the snapshot expires.
        """
        try:
            await self._delete(built)
        except Exception as error:
            logger.warning("Run %s could not delete a late setup snapshot: %s", self.run_id, error)
            raise SandboxFatalError("The run ended; its late setup snapshot was not deleted.") from error

    def _require_open(self) -> None:
        """Refuse work after the run ended.

        Raises:
            SandboxFatalError: The cache is closed.
        """
        if self._closed:
            raise SandboxFatalError("The run ended; its setup snapshots are gone.")

    def _touch(self, cache_key: str) -> BuiltSnapshot | None:
        """Return a cached snapshot and mark it as used.

        Args:
            cache_key: The setup cache key.

        Returns:
            The snapshot, or ``None`` when the key has none.
        """
        built = self._entries.get(cache_key)
        if built is not None:
            self._last_used[cache_key] = self._clock.monotonic()
        return built

    @contextlib.asynccontextmanager
    async def _cross_pod_lock(self, cache_key: str) -> AsyncIterator[None]:
        """Hold the Postgres advisory lock of a key for the whole build.

        The lock is transaction scoped, which works behind pgbouncer; closing
        the connection rolls the transaction back and frees the lock.

        Args:
            cache_key: The setup cache key.

        Yields:
            Nothing; the lock is held inside the block.

        Raises:
            SnapshotLockTimeoutError: The lock stayed taken past ``LOCK_WAIT``.
        """
        if self._engine is None or self._engine.dialect.name != "postgresql":
            yield
            return
        key = advisory_lock_key(self.run_id, cache_key)
        conn = await asyncio.to_thread(self._engine.connect)
        try:
            deadline = self._clock.monotonic() + LOCK_WAIT
            while not await asyncio.to_thread(_try_lock, conn, key):
                if self._clock.monotonic() >= deadline:
                    raise SnapshotLockTimeoutError("Another pod held the setup snapshot lock for too long.")
                await self._clock.sleep(LOCK_POLL)
            yield
        finally:
            await asyncio.shield(asyncio.to_thread(conn.close))


def _try_lock(conn: Connection, key: int) -> bool:
    """Try once to take a transaction-scoped advisory lock.

    A failed try rolls its transaction back, so the next try starts clean.

    Args:
        conn: The connection that holds the lock.
        key: The advisory lock key.

    Returns:
        Whether the lock is now held by this connection.
    """
    if not conn.in_transaction():
        conn.begin()
    acquired = bool(conn.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}).scalar())
    if not acquired:
        conn.rollback()
    return acquired

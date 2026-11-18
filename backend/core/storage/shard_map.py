"""The step log shard map: each run's step log lives on one database shard (SKY-731).

``run_id`` is the shard key, so a run coordinator reads all its agents'
steps from one database. The design follows the Durable Object model: a run
is one key with one home, and more shards give more scale. The main
database is always the shard named ``main``; ``STEP_LOG_SHARDS`` adds more.

With no extra shard, every run uses the main database. The map writes
nothing, and the first flush of a run on the main database records ``main``
for it. With extra shards, the first use of a run records its shard in
``jobs.step_log_shard`` with one conditional UPDATE. The record never
changes, so a new shard gets only new runs. A run that already has steps on
the main database, or a ``main`` record, keeps ``main``.

A shard holds only ``agent_steps``, ``tree_objects`` and ``storage_packs``.
Jobs, users, wallets and budgets stay on the main database. The cascade from
``jobs`` stops at the main database, so a job delete and an account purge
delete the rows on every other shard themselves (``tree_purge``, SKY-733).

Give every pod the same ``STEP_LOG_SHARDS`` map before any run records a
shard. The check is for each run: a pod refuses to read or write a run that
records a shard that its map does not have (:class:`UnroutableRunError`),
and the flush fence refuses a step whose run records another shard. A pod
with no extra shard also refuses to start when a job records a shard other
than ``main``.
"""

from __future__ import annotations

import re
import threading
import zlib
from collections.abc import Mapping

from sqlalchemy import case, create_engine, exists, select, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from ..config import settings
from .models import AgentStepModel, JobModel

MAIN = "main"
SHARD_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# A cached entry is a run id and a name, about 0.2 KB: the cap keeps the cache near 20 MB.
CACHE_MAX = 100_000
SHARD_POOL_SIZE = 5
SHARD_POOL_OVERFLOW = 5
# A shard that hangs must not hold a flush, or the ``jobs`` row locks behind it, without limit.
SHARD_CONNECT_TIMEOUT_SECONDS = 10
SHARD_STATEMENT_TIMEOUT_MS = 30_000
# The statement timeout runs on the server, so it does not end a wait on a connection that the network cut.
# These client-side TCP limits drop such a connection after about the same 30 seconds.
SHARD_TCP_USER_TIMEOUT_MS = 30_000
SHARD_KEEPALIVES_IDLE_SECONDS = 10
SHARD_KEEPALIVES_INTERVAL_SECONDS = 5
SHARD_KEEPALIVES_COUNT = 4


class UnroutableRunError(RuntimeError):
    """The run records a step log shard that this pod's map does not have.

    It stays so until a deploy changes the map, so a retry cannot help.
    """


class ShardMap:
    """Route each run's step log reads and writes to the run's shard."""

    def __init__(self, main: Engine, shards: Mapping[str, Engine] | None = None) -> None:
        """Bind the main database and the extra shards.

        Args:
            main: The main database; it holds ``jobs`` and is the shard ``main``.
            shards: The extra shard engines by name.

        Raises:
            ValueError: When a name is ``main`` or not a short lowercase id.
        """
        extra = dict(shards or {})
        for name in extra:
            if name == MAIN or not SHARD_NAME.fullmatch(name):
                raise ValueError(f"bad step log shard name: {name!r}")
        self._main = main
        self._engines = {MAIN: main, **extra}
        self._names = tuple(sorted(self._engines))
        self._owned: tuple[Engine, ...] = ()
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}

    @classmethod
    def from_settings(cls, main: Engine) -> ShardMap:
        """Build the map from ``STEP_LOG_SHARDS``.

        Args:
            main: The main database.

        Returns:
            The map; with no configured shard, a map of the main database only.
            :meth:`dispose` closes the shard engines that it creates.
        """
        shards = {name: shard_engine(url.get_secret_value()) for name, url in settings.step_log_shards.items()}
        built = cls(main, shards)
        built._owned = tuple(shards.values())
        return built

    def dispose(self) -> None:
        """Close the connection pools of the shard engines that :meth:`from_settings` created."""
        for engine in self._owned:
            engine.dispose()

    @property
    def main(self) -> Engine:
        """Return the main database engine."""
        return self._main

    @property
    def single(self) -> bool:
        """Return whether the main database is the only shard."""
        return len(self._engines) == 1

    def names(self) -> tuple[str, ...]:
        """Return every shard name, sorted."""
        return self._names

    def engine(self, name: str) -> Engine:
        """Return the engine of a shard.

        Args:
            name: The shard name.

        Returns:
            The engine.

        Raises:
            KeyError: When no shard has the name.
        """
        try:
            return self._engines[name]
        except KeyError:
            raise KeyError(f"unknown step log shard: {name!r}") from None

    def shard_of(self, run_id: str) -> str:
        """Return the run's shard, and record it on the run's first use.

        Args:
            run_id: The job id.

        Returns:
            The shard name. A run with no job row gets ``main``, and nothing
            is recorded: the fence fails its steps.

        Raises:
            UnroutableRunError: When the run records a shard that this map does not have.
        """
        with self._lock:
            name = self._cache.get(run_id)
        if name is not None:
            return name
        if self.single:
            return self._single_shard_of(run_id)
        pick = self._names[zlib.crc32(run_id.encode("utf-8", "replace")) % len(self._names)]
        has_steps = exists().where(AgentStepModel.run_id == run_id)
        with Session(self._main) as session:
            # The IS NULL guard makes the first writer win; a second writer reads the first one's shard.
            session.execute(
                update(JobModel)
                .where(JobModel.optimization_id == run_id, JobModel.step_log_shard.is_(None))
                .values(step_log_shard=case((has_steps, MAIN), else_=pick))
                .execution_options(synchronize_session=False)
            )
            name = session.scalar(select(JobModel.step_log_shard).where(JobModel.optimization_id == run_id))
            session.commit()
        if name is None:
            return MAIN
        if name not in self._engines:
            raise UnroutableRunError(f"run {run_id} records step log shard {name!r}, which this pod does not have")
        self._remember(run_id, name)
        return name

    def _single_shard_of(self, run_id: str) -> str:
        """Return ``main`` for a run on a map with no extra shard, after a check of the run's record.

        Another pod with a shard map can record a shard for the run at any time
        before the run's first step lands, so only a ``main`` record is cached.

        Args:
            run_id: The job id.

        Returns:
            ``main``.

        Raises:
            UnroutableRunError: When the run records another shard.
        """
        with Session(self._main) as session:
            name = session.scalar(select(JobModel.step_log_shard).where(JobModel.optimization_id == run_id))
        if name is None:
            return MAIN
        if name != MAIN:
            raise UnroutableRunError(
                f"run {run_id} records step log shard {name!r}, but STEP_LOG_SHARDS is empty on this pod; "
                "give every pod the same map"
            )
        self._remember(run_id, name)
        return name

    def _remember(self, run_id: str, name: str) -> None:
        """Cache a run's recorded shard, which never changes, and drop the oldest entry above ``CACHE_MAX``.

        Args:
            run_id: The job id.
            name: The recorded shard name.
        """
        with self._lock:
            self._cache[run_id] = name
            if len(self._cache) > CACHE_MAX:
                del self._cache[next(iter(self._cache))]

    def check(self) -> None:
        """Refuse a map with no extra shard when a job records another shard; a map with shards passes.

        A worker calls it before it takes work, so a pod with a wrong map stops
        at start. A run that records a shard later is refused by :meth:`shard_of`
        and the flush fence.

        Raises:
            RuntimeError: With no extra shard, when a job records a shard other than ``main``.
        """
        if not self.single:
            return
        with Session(self._main) as session:
            run_id = session.scalar(
                select(JobModel.optimization_id)
                .where(JobModel.step_log_shard.is_not(None), JobModel.step_log_shard != MAIN)
                .limit(1)
            )
        if run_id is not None:
            raise RuntimeError(
                f"run {run_id} records a step log shard, but STEP_LOG_SHARDS is empty on this pod; "
                "give every pod the same map"
            )

    def engine_for(self, run_id: str) -> Engine:
        """Return the engine of the run's shard.

        Args:
            run_id: The job id.

        Returns:
            The engine.

        Raises:
            UnroutableRunError: When the run records a shard that this map does not have.
        """
        return self.engine(self.shard_of(run_id))

    def for_run(self, run_id: str) -> Session:
        """Return a new session on the run's shard; the caller closes it.

        Args:
            run_id: The job id.

        Returns:
            The session.

        Raises:
            UnroutableRunError: When the run records a shard that this map does not have.
        """
        return Session(self.engine_for(run_id))


def shard_engine(url: str, *, pooled: bool = True) -> Engine:
    """Create the engine of one shard, with server-side and TCP time limits on PostgreSQL.

    Args:
        url: The shard database URL.
        pooled: ``False`` gives a ``NullPool`` engine, for a one-time use such as a boot migration.

    Returns:
        The engine.
    """
    options: dict[str, object] = {"pool_pre_ping": True} if pooled else {"poolclass": NullPool}
    if make_url(url).get_backend_name() == "postgresql":
        if pooled:
            options.update(pool_size=SHARD_POOL_SIZE, max_overflow=SHARD_POOL_OVERFLOW)
        options.update(
            connect_args={
                "connect_timeout": SHARD_CONNECT_TIMEOUT_SECONDS,
                "options": f"-c statement_timeout={SHARD_STATEMENT_TIMEOUT_MS}",
                "tcp_user_timeout": SHARD_TCP_USER_TIMEOUT_MS,
                "keepalives": 1,
                "keepalives_idle": SHARD_KEEPALIVES_IDLE_SECONDS,
                "keepalives_interval": SHARD_KEEPALIVES_INTERVAL_SECONDS,
                "keepalives_count": SHARD_KEEPALIVES_COUNT,
            },
        )
    return create_engine(url, **options)

"""The step log shard map: each run's step log lives on one database shard (SKY-731).

``run_id`` is the shard key, so a run coordinator reads all its agents'
steps from one database. The design follows the Durable Object model: a run
is one key with one home, and more shards give more scale. The main
database is always the shard named ``main``; ``STEP_LOG_SHARDS`` adds more.

With no extra shard, every run uses the main database and the map reads and
writes nothing, as before the map. With extra shards, the first use of a
run records its shard in ``jobs.step_log_shard`` with one conditional
UPDATE. The record never changes, so a new shard gets only new runs. A run
that already has steps on the main database records ``main``.

A shard holds only ``agent_steps``, ``tree_objects`` and ``storage_packs``.
Jobs, users, wallets and budgets stay on the main database. The cascade from
``jobs`` stops at the main database: a job or account delete leaves its rows
on the other shards until SKY-733 (tree retention and purge) adds the purge.
"""

from __future__ import annotations

import re
import threading
import zlib
from collections.abc import Mapping

from sqlalchemy import case, create_engine, exists, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..config import settings
from .models import AgentStepModel, JobModel

MAIN = "main"
SHARD_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# A cached entry is a run id and a name, about 0.2 KB: the cap keeps the cache near 20 MB.
CACHE_MAX = 100_000
SHARD_POOL_SIZE = 5
SHARD_POOL_OVERFLOW = 5


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
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}

    @classmethod
    def from_settings(cls, main: Engine) -> ShardMap:
        """Build the map from ``STEP_LOG_SHARDS``.

        Args:
            main: The main database.

        Returns:
            The map; with no configured shard, a map of the main database only.
        """
        shards = {
            name: create_engine(url, pool_pre_ping=True, pool_size=SHARD_POOL_SIZE, max_overflow=SHARD_POOL_OVERFLOW)
            for name, url in settings.step_log_shards.items()
        }
        return cls(main, shards)

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
            KeyError: When the recorded shard is not configured.
        """
        if self.single:
            return MAIN
        with self._lock:
            name = self._cache.get(run_id)
        if name is not None:
            return name
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
        self.engine(name)
        with self._lock:
            self._cache[run_id] = name
            if len(self._cache) > CACHE_MAX:
                del self._cache[next(iter(self._cache))]
        return name

    def engine_for(self, run_id: str) -> Engine:
        """Return the engine of the run's shard.

        Args:
            run_id: The job id.

        Returns:
            The engine.
        """
        return self.engine(self.shard_of(run_id))

    def for_run(self, run_id: str) -> Session:
        """Return a new session on the run's shard; the caller closes it.

        Args:
            run_id: The job id.

        Returns:
            The session.
        """
        return Session(self.engine_for(run_id))

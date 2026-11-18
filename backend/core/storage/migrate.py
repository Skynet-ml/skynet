"""Keep an adopted database's Alembic version in step at boot.

Production builds its schema with ``Base.metadata.create_all`` and then tracks it
with Alembic, because ``create_all`` never ALTERs an existing table: a migration
that adds a column to a table an earlier boot already created would otherwise
never land — exactly the drift that silently stranded ``billing_provider_keys``.

On an adopted database the pending migrations run *before* ``create_all``: run
after it, a migration that creates or renames a table collides with the empty
table ``create_all`` just made from the new models (the #502 deploy failed on
``wallet_ledger_id_seq`` this way). On a database Alembic has never stamped, the
``create_all`` schema already *is* head, so it is stamped afterwards rather than
replayed (the pgvector baseline migration would fail where the extension is
absent). Both steps run under the same advisory lock as ``create_all`` so
concurrent replicas don't race, and are Postgres-only — SQLite test stores get
``None`` from the lock helper and skip Alembic, since their schema comes
straight from the ORM models.

The step log shard databases (SKY-731) migrate after the main transaction
commits and its lock is released: a shard that hangs or fails cannot hold the
main lock or roll back the main migrations. A shard failure fails the boot,
and the next boot runs the shard migrations again; they are idempotent.
Each shard migration holds an advisory lock on its shard, so pods that boot
at the same time migrate a shard one after the other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from alembic import command

from ..config import settings
from .schema_lock import schema_bootstrap_lock
from .shard_map import shard_engine

# backend/core/storage/migrate.py -> backend/alembic.ini
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
# The chain of the step log shard databases. It is not in ``versions`` so the main chain keeps one head.
_STEP_LOG_VERSIONS = _ALEMBIC_INI.parent / "alembic" / "step_log_versions"
# Continues the 7421370000xx advisory-lock series; SKY-733's tree compactor sweep takes ...09.
STEP_LOG_SHARD_MIGRATION_LOCK_KEY = 742137000010
# The shard engine's 30-second runtime statement limit is too short for DDL that waits on the lock of another pod.
SHARD_MIGRATION_STATEMENT_TIMEOUT_MS = 600_000
# A DDL statement that waits on a table lock blocks each later query of that table, so the wait has a short limit.
SHARD_MIGRATION_LOCK_TIMEOUT_MS = 30_000


def upgrade_if_adopted(engine: Any) -> None:
    """Apply pending migrations when Alembic already tracks this database.

    Call before ``create_all`` so new tables come from their migrations, not from
    the models.

    Args:
        engine: The store's SQLAlchemy engine. On non-PostgreSQL dialects the lock
            helper yields ``None`` and this returns without touching Alembic.
    """
    with schema_bootstrap_lock(engine) as conn:
        upgraded = conn is not None and _is_adopted(conn)
        if upgraded:
            command.upgrade(_alembic_config(conn), "head")
    if upgraded:
        upgrade_step_log_shards()


def stamp_if_unadopted(engine: Any) -> None:
    """Stamp head on a database Alembic has never tracked.

    Call after ``create_all``, whose schema on such a database already is head.

    Args:
        engine: The store's SQLAlchemy engine. On non-PostgreSQL dialects the lock
            helper yields ``None`` and this returns without touching Alembic.
    """
    with schema_bootstrap_lock(engine) as conn:
        stamped = conn is not None and not _is_adopted(conn)
        if stamped:
            command.stamp(_alembic_config(conn), "head")
    if stamped:
        upgrade_step_log_shards()


def sync_migration_head(engine: Any) -> None:
    """Upgrade an adopted database, else stamp it at head.

    Args:
        engine: The store's SQLAlchemy engine.
    """
    upgrade_if_adopted(engine)
    stamp_if_unadopted(engine)


def upgrade_step_log_shards() -> None:
    """Bring every ``STEP_LOG_SHARDS`` database to the head of the step log shard chain.

    Each shard has its own ``alembic_version`` table and its own transaction,
    on an engine with the shard connect and TCP limits. On PostgreSQL the
    transaction first takes the shard's migration advisory lock and sets the
    migration statement and lock timeouts. Call it with no main transaction
    open, so a slow shard holds no main lock.

    Raises:
        RuntimeError: When a shard migration fails; it names the shard.
    """
    script = ScriptDirectory(str(_STEP_LOG_VERSIONS.parent), version_locations=[str(_STEP_LOG_VERSIONS)])
    for name, url in sorted(settings.step_log_shards.items()):
        engine = shard_engine(url.get_secret_value(), pooled=False)
        try:
            with engine.begin() as connection:
                if connection.dialect.name == "postgresql":
                    connection.execute(text(f"SET LOCAL statement_timeout = {SHARD_MIGRATION_STATEMENT_TIMEOUT_MS}"))
                    # Before the lock timeout: a pod waits for the whole migration of another pod, not 30 seconds.
                    connection.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": STEP_LOG_SHARD_MIGRATION_LOCK_KEY}
                    )
                    connection.execute(text(f"SET LOCAL lock_timeout = {SHARD_MIGRATION_LOCK_TIMEOUT_MS}"))
                # Not command.upgrade: inside env.py, a second EnvironmentContext removes the alembic.context proxy.
                # ``_upgrade_revs`` is private, but alembic 1.18.4 (pinned in uv.lock) has no public way to get the
                # upgrade steps, and its own command.upgrade calls the same method. An alembic upgrade can rename it:
                # test_shard_map.py checks it, so the test fails before a boot does.
                migration = MigrationContext.configure(
                    connection, opts={"script": script, "fn": lambda rev, _: script._upgrade_revs("head", rev)}
                )
                with Operations.context(migration):
                    migration.run_migrations()
        except Exception as error:
            raise RuntimeError(f"step log shard {name} migration failed; the main database is migrated") from error
        finally:
            engine.dispose()


def cli_upgrades_shards(cmd_opts: Any) -> bool:
    """Return whether an ``alembic`` CLI command also upgrades the step log shards.

    Only ``upgrade`` does: a CLI ``downgrade``, ``stamp`` or ``check`` acts on the main chain alone.

    Args:
        cmd_opts: The parsed CLI options of the Alembic config, or ``None`` for a call from code.

    Returns:
        ``True`` for an ``upgrade`` command and for a call from code with no CLI options.
    """
    command_line = getattr(cmd_opts, "cmd", None) if cmd_opts is not None else None
    return command_line is None or command_line[0].__name__ == "upgrade"


def _alembic_config(conn: Any) -> Config:
    """Build an Alembic config that runs on the lock-holding connection.

    Sharing that connection keeps migrations in the locked transaction rather
    than a second, unserialized session.

    Args:
        conn: A live connection bound to the bootstrap transaction.

    Returns:
        The Alembic config bound to ``conn``.
    """
    config = Config(str(_ALEMBIC_INI))
    config.attributes["connection"] = conn
    # Keep env.py from running fileConfig(alembic.ini): that replaces the
    # root handlers and raises the root level to WARN, silencing every app
    # INFO log (JSON format included) for the rest of the process lifetime.
    config.attributes["configure_logger"] = False
    return config


def _is_adopted(conn: Any) -> bool:
    """Return whether ``alembic_version`` exists and already holds a revision.

    Args:
        conn: A live connection bound to the bootstrap transaction.

    Returns:
        ``True`` once Alembic has stamped this database — so pending migrations
        should be applied rather than the freshly built schema stamped at head.
    """
    if not inspect(conn).has_table("alembic_version"):
        return False
    return conn.execute(text("SELECT 1 FROM alembic_version LIMIT 1")).first() is not None

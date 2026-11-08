"""PostgreSQL database backend for job storage.

Provides RemoteDBJobStore for persisting job state to a PostgreSQL database.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import threading
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import Engine, and_, case, create_engine, func, or_, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased, defer, sessionmaker

from .. import run_log
from ..billing.budgets import BudgetConflictError, BudgetError, BudgetInFlightError, BudgetService
from ..billing.plans import has_pro_entitlement
from ..config import settings
from ..constants import (
    OPTIMIZATION_TYPE_TAGGING,
    PAYLOAD_OVERVIEW_DATASET_ROWS,
    PAYLOAD_OVERVIEW_MODEL_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE,
    PAYLOAD_OVERVIEW_OPTIMIZER_NAME,
    PAYLOAD_OVERVIEW_TOTAL_PAIRS,
    PROGRESS_CANDIDATE,
    PROGRESS_LANE_STARTED,
    PROGRESS_REJECTED,
    STRUCTURAL_PROGRESS_EVENTS,
    TQDM_KEY_PREFIX,
    WORKER_RESTART_CAUSE,
)
from ..worker.checkpoint_compat import CheckpointCompatibilityError, validate_checkpoint
from .agent_run_store import PostgresAgentRunStore
from .base import JobRecord, LogEntryRecord, ProgressEventRecord
from .checkpoint_store import GepaCheckpoint, PostgresCheckpointBlobStore, PostgresGridPairResultStore
from .log_quota import LogQuotaGate
from .migrate import stamp_if_unadopted, upgrade_if_adopted
from .models import (
    EMBEDDING_DIM,
    AgentStagedDatasetModel,
    Base,
    BillingCustomerModel,
    BlackboxAgentRunModel,
    ConversationEmbeddingModel,
    GepaCheckpointModel,
    GridPairResultModel,
    JobEmbeddingModel,
    JobModel,
    LogEntryModel,
    MonthlyActiveUserModel,
    OptimizationShareGrantModel,
    ProgressEventModel,
    RunCallModel,
    RunCandidateModel,
    RunFolderItemModel,
    UserModel,
    UserQuotaAuditModel,
    UserQuotaOverrideModel,
    UserStorageQuotaOverrideModel,
)
from .run_call_store import PROGRAM_EVENT_KEY, PostgresRunCallStore, event_candidate_key
from .schema_lock import schema_bootstrap_lock
from .usage import (
    StorageItem,
    StorageUsage,
    compute_user_storage,
    compute_user_storage_category_items,
    compute_user_storage_items,
    imported_job_extra_bytes,
    json_byte_size,
)


def _mark_resume_budget_stop(job: JobModel) -> None:
    """Stop a resume that the remaining authorized budget cannot fund.

    Args:
        job: Locked run row to terminate.
    """
    job.status = "stopped"
    job.stop_reason = "budget_reached"
    job.result_availability = "none"
    job.terminal_evidence = {
        **(job.terminal_evidence or {}),
        "candidate_origin": None,
        "final_evaluation_completed": False,
        "final_evaluation_reason": "budget_reached",
    }
    job.completed_at = datetime.now(UTC)
    job.message = "The remaining authorized budget cannot cover resuming this run."
    job.recovery = None


logger = logging.getLogger(__name__)


def _usage_wait_start(recovery: dict[str, Any]) -> datetime | None:
    """Read when a run's current usage wait window started.

    Args:
        recovery: The run's ``waiting_for_usage`` recovery state.

    Returns:
        The aware start time, or ``None`` when it is missing or invalid.
    """
    try:
        since = datetime.fromisoformat(str(recovery["waiting_since"]))
    except (KeyError, ValueError):
        return None
    return since if since.tzinfo is not None else None


def _usage_wait_expired(recovery: Any, now: datetime) -> bool:
    """Tell whether an orphaned run waited too long for its in-flight usage.

    Args:
        recovery: The run's stored recovery state.
        now: Time of the current sweep.

    Returns:
        Whether the run's usage made no settlement progress for
        ``ORPHANED_USAGE_WAIT``. A wait with no valid ``waiting_since`` is not
        expired; the next requeue attempt records the start of its window.
    """
    if not isinstance(recovery, dict) or recovery.get("phase") != "waiting_for_usage":
        return False
    since = _usage_wait_start(recovery)
    return since is not None and now - since >= ORPHANED_USAGE_WAIT


def _track_usage_wait(recovery: dict[str, Any], budget: Any) -> dict[str, Any] | None:
    """Record the in-flight usage a waiting run still has, and restart its window on progress.

    Args:
        recovery: The run's ``waiting_for_usage`` recovery state.
        budget: Current snapshot of the run's budget.

    Returns:
        The updated recovery state, or ``None`` when nothing changed.
    """
    observed = [int(budget.pending_operations or 0), int(budget.reserved_cents or 0)]
    previous = recovery.get("pending_usage")
    progressed = (
        isinstance(previous, list)
        and len(previous) == 2
        and any(now_value < before for now_value, before in zip(observed, previous, strict=True))
    )
    updated = {**recovery, "pending_usage": observed}
    if progressed or _usage_wait_start(recovery) is None:
        updated["waiting_since"] = datetime.now(UTC).isoformat()
    return None if updated == recovery else updated


MAX_PROGRESS_EVENTS = 5000
ORPHANED_RUN_MESSAGE = "The run stopped because its worker went away. Resume, retry or clone it to continue."
ORPHANED_RUN_RECOVERY_FAILED_MESSAGE = (
    "The run stopped because its worker went away, and it cannot continue by itself. Retry or clone it."
)
# Usage that makes no settlement progress in this window may never settle, and
# the run must not stay "running" with no worker while it waits. The window
# restarts each time the run's pending usage shrinks. The stopped run keeps the
# interrupted budget generation. A manual resume of it waits for that usage, then
# waives the work the resume repeats, as the automatic requeue does: the work
# after the earliest checkpoint, or all of that generation when a top-level run
# with no checkpoint starts again. Only a stop that a worker restart caused is
# exempt from the attempt count and cap; a crash stop still counts.
ORPHANED_USAGE_WAIT = timedelta(minutes=15)
# Guards each store's in-memory list of runs the orphan sweep ended; test
# stores skip ``__init__``, so the list is created on first use.
_ENDED_ORPHANS_LOCK = threading.Lock()
PROGRESS_TRIM_SAMPLE_RATE = 100
# Auto's composition phases; a single-engine run's lane reports phase "single".
AUTO_LANE_PHASES = frozenset({"explore", "continue"})
_IMMUTABLE_JOB_COLUMNS = frozenset({"optimization_id", "notified_at", "notification_claims", "idempotency_key"})
# The JSON columns whose serialized size dominates a job's storage footprint and
# therefore make up ``jobs.stored_bytes``. ``latest_metrics`` / ``message`` are
# tiny and intentionally excluded to keep the recompute read narrow.
_STORED_BYTES_JSON_COLUMNS = ("payload", "result", "payload_overview")
# The recompute also reads the two columns an imported run counts (SKY-117).
_STORED_BYTES_EXTRA_COLUMNS = ("latest_metrics", "terminal_evidence")
_STORED_BYTES_READ_COLUMNS = tuple(
    getattr(JobModel, col) for col in (*_STORED_BYTES_JSON_COLUMNS, *_STORED_BYTES_EXTRA_COLUMNS)
)


def _recomputed_stored_bytes(stored_row: Any, update_values: dict[str, Any]) -> int:
    """Return a job's ``stored_bytes`` after an update, from its stored row and the new values.

    Args:
        stored_row: The row read with ``_STORED_BYTES_READ_COLUMNS``, in that order.
        update_values: Column values the update writes.

    Returns:
        The size of the main JSON columns, plus the metrics and terminal evidence
        of a run imported from the desktop.
    """
    current = dict(zip((*_STORED_BYTES_JSON_COLUMNS, *_STORED_BYTES_EXTRA_COLUMNS), stored_row, strict=True))
    current.update({col: value for col, value in update_values.items() if col in current})
    return sum(json_byte_size(current[col]) for col in _STORED_BYTES_JSON_COLUMNS) + imported_job_extra_bytes(
        current["payload_overview"], current["latest_metrics"], current["terminal_evidence"]
    )


def _log_record(log: LogEntryModel) -> LogEntryRecord:
    """Shape one ``job_logs`` row as the record the API and the live stream serve.

    Args:
        log: The stored row.

    Returns:
        The row's fields, with a pre-structured row's missing source read as ``host``.
    """
    return cast(
        LogEntryRecord,
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "level": log.level,
            "logger": log.logger,
            "message": log.message,
            "pair_index": log.pair_index,
            "source": log.source or "host",
            "event": log.event,
            "fields": log.fields,
            "candidate": log.candidate,
            "case": log.case_id,
        },
    )


def _build_connect_args(db_url: str) -> dict[str, Any]:
    """Build DBAPI connection args for the configured SQLAlchemy driver.

    Args:
        db_url: SQLAlchemy database URL used to infer the selected driver.

    Returns:
        Keyword arguments passed through SQLAlchemy to the DBAPI connect call.
    """
    connect_args: dict[str, Any] = {"options": "-c timezone=UTC"}
    driver = make_url(db_url).drivername
    # libpq-based drivers honour TCP keepalive params so a connection silently
    # dropped by a load balancer / NAT idle timeout is detected and recycled
    # instead of surfacing as a stall on the next checkout from the pool.
    if driver in {"postgresql", "postgresql+psycopg2", "postgresql+psycopg", "postgresql+psycopg3"}:
        connect_args.update(
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )

    if not settings.db_pgbouncer_transaction_mode:
        return connect_args

    if driver in {"postgresql+psycopg", "postgresql+psycopg3"}:
        connect_args["prepare_threshold"] = None
    elif driver == "postgresql+asyncpg":
        connect_args["prepared_statement_cache_size"] = 0
    return connect_args


def _build_engine_kwargs(db_url: str) -> dict[str, Any]:
    """Return SQLAlchemy engine options sourced from settings.

    Args:
        db_url: SQLAlchemy database URL used for driver-specific connect args.

    Returns:
        Engine keyword arguments for :func:`sqlalchemy.create_engine`.
    """
    return {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_pool_max_overflow,
        "pool_recycle": settings.db_pool_recycle_seconds,
        "pool_timeout": settings.db_pool_timeout_seconds,
        "connect_args": _build_connect_args(db_url),
    }


def _json_int(value: Any) -> int | None:
    """Coerce one extracted JSON scalar to ``int``, or ``None`` when it isn't one.

    Extraction returns text on PostgreSQL (``->>``) and native affinity values
    on SQLite, so both forms must coerce; junk degrades to ``None``, mirroring
    the ``isinstance`` guards the analytics aggregations always applied.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_float(value: Any) -> float | None:
    """Coerce one extracted JSON scalar to ``float``, or ``None`` when it isn't one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_str(value: Any) -> str | None:
    """Keep one extracted JSON scalar when it is a string, else ``None``.

    Both dialects hand string members back as ``str``; anything else means the
    key held a number or a nested value, which the summaries never label with.
    """
    return value if isinstance(value, str) else None


def _result_summary_columns() -> tuple[Any, ...]:
    """SELECT expressions extracting the ``result`` scalars list rows carry.

    These are the only ``result`` members any list surface reads — dashboard
    cards (``build_summary``), the KPI rollups and the sidebar. Everything
    else in the blob (per-example outputs, program artifacts, demos — the
    multi-MB part) is only ever read off a single-job fetch, so the list
    SELECTs project just these and leave the blob in the database. Positional
    order is the contract with :func:`_assemble_result_summary`. Uses
    SQLAlchemy's dialect-portable JSON operators so the same projection runs
    on PostgreSQL (``->>``) and the SQLite test harness (``json_extract``).

    Returns:
        A ``result IS NULL`` flag followed by the extracted scalars.
    """
    result = JobModel.result
    best_pair = result["best_pair"]
    return (
        result.is_(None),
        result["baseline_test_metric"].as_string(),
        result["optimized_test_metric"].as_string(),
        result["runtime_seconds"].as_string(),
        result["completed_pairs"].as_string(),
        result["failed_pairs"].as_string(),
        best_pair["baseline_test_metric"].as_string(),
        best_pair["optimized_test_metric"].as_string(),
        best_pair["runtime_seconds"].as_string(),
        best_pair["generation_model"].as_string(),
        best_pair["reflection_model"].as_string(),
        result["baseline_score"].as_string(),
        result["best_score"].as_string(),
    )


def _assemble_result_summary(values: Any) -> dict[str, Any] | None:
    """Rebuild a pruned ``result`` dict from :func:`_result_summary_columns` scalars.

    Keeps the shapes the readers rely on: a NULL ``result`` stays ``None``
    (the ``if not result_data`` guards), absent or non-scalar keys stay
    absent (``.get`` → ``None``, as on the full blob), and ``best_pair``
    appears only when at least one of its summarized keys parsed — matching
    how a grid row looks to ``build_summary``.

    Args:
        values: The positional scalars, in :func:`_result_summary_columns` order.

    Returns:
        The pruned ``result`` dict, or ``None`` for a result-less row.
    """
    (
        result_null,
        r_baseline,
        r_optimized,
        r_runtime,
        r_completed,
        r_failed,
        bp_baseline,
        bp_optimized,
        bp_runtime,
        bp_generation,
        bp_reflection,
        r_baseline_score,
        r_best_score,
    ) = values
    if result_null:
        return None
    result_pairs = (
        ("baseline_test_metric", _json_float(r_baseline)),
        ("optimized_test_metric", _json_float(r_optimized)),
        ("runtime_seconds", _json_float(r_runtime)),
        ("completed_pairs", _json_int(r_completed)),
        ("failed_pairs", _json_int(r_failed)),
        ("baseline_score", _json_float(r_baseline_score)),
        ("best_score", _json_float(r_best_score)),
    )
    result: dict[str, Any] = {key: value for key, value in result_pairs if value is not None}
    best_pair_pairs = (
        ("baseline_test_metric", _json_float(bp_baseline)),
        ("optimized_test_metric", _json_float(bp_optimized)),
        ("runtime_seconds", _json_float(bp_runtime)),
        ("generation_model", _json_str(bp_generation)),
        ("reflection_model", _json_str(bp_reflection)),
    )
    best_pair = {key: value for key, value in best_pair_pairs if value is not None}
    if best_pair:
        result["best_pair"] = best_pair
    return result


def _assemble_analytics_row(row: Any) -> JobRecord:
    """Rebuild one skinny analytics job dict from its extracted JSON scalars.

    Keeps the shapes the aggregations rely on: absent overview keys stay
    absent (``.get`` → ``None``); ``result`` is rebuilt by
    :func:`_assemble_result_summary` from the trailing scalars.

    Args:
        row: The SELECT tuple from :meth:`RemoteDBJobStore.scan_jobs_for_analytics`.

    Returns:
        A ``JobRecord``-shaped dict with pruned ``payload_overview``/``result``.
    """
    optimization_id, job_status, ov_optimizer, ov_model, ov_type, ov_rows, ov_pairs = row[:7]
    overview_pairs = (
        (PAYLOAD_OVERVIEW_OPTIMIZER_NAME, ov_optimizer),
        (PAYLOAD_OVERVIEW_MODEL_NAME, ov_model),
        (PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE, ov_type),
        (PAYLOAD_OVERVIEW_DATASET_ROWS, _json_int(ov_rows)),
        (PAYLOAD_OVERVIEW_TOTAL_PAIRS, _json_int(ov_pairs)),
    )
    return {
        "optimization_id": optimization_id,
        "status": job_status,
        "payload_overview": {key: value for key, value in overview_pairs if value is not None},
        "result": _assemble_result_summary(row[7:]),
    }


def _user_facing_jobs():
    """SQL filter keeping only user-facing job rows.

    Tagger bulk auto-tag jobs share the jobs table so the worker fleet can
    claim them, but they are not optimization runs: every listing and count
    surface skips them unless a caller filters for the type explicitly. The
    NULL branch is kept because ``optimization_type`` is nullable (legacy
    rows) and a bare ``!=`` would silently drop those rows too. Distributed
    grid-pair child rows are scheduling internals of their parent grid and
    are excluded the same way (see also :func:`_top_level_jobs`, applied
    even when a caller filters by type).

    Returns:
        A SQLAlchemy boolean clause for ``query.filter``.
    """
    return and_(
        or_(
            JobModel.optimization_type.is_(None),
            JobModel.optimization_type != OPTIMIZATION_TYPE_TAGGING,
        ),
        _top_level_jobs(),
    )


def _top_level_jobs():
    """SQL filter excluding distributed grid-pair child rows.

    Child rows share the jobs table (so the claim/lease/orphan machinery
    applies to them verbatim) but belong to their parent grid in every
    user-facing sense: listings, counts, and dashboards must never show
    them, even when the caller filters for ``grid_search`` explicitly.

    Returns:
        A SQLAlchemy boolean clause for ``query.filter``.
    """
    return JobModel.parent_optimization_id.is_(None)


# Must match PG_CONTAINER in desktop/src/main/postgres.ts.
_LOCAL_PG_CONTAINER = "skynet-local-postgres"
_LOCAL_PG_DATA = "/var/lib/postgresql/data"


def _local_postgres_free_bytes() -> int | None:
    """Return the free bytes of the Docker disk under the Mac app's Postgres volume.

    Returns:
        The free bytes, or ``None`` when Docker or the container is not there
        (for example a local-mode backend that a developer runs by hand).
    """
    docker = shutil.which("docker")
    if docker is None:
        return None
    try:
        result = subprocess.run(
            [docker, "exec", _LOCAL_PG_CONTAINER, "df", "-B1", "--output=avail", _LOCAL_PG_DATA],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.split()
    if result.returncode != 0 or not lines or not lines[-1].isdigit():
        return None
    return int(lines[-1])


class RemoteDBJobStore:
    """PostgreSQL-backed job storage using SQLAlchemy.

    No threading lock needed — PostgreSQL handles concurrent access natively.
    """

    def __init__(self, db_url: str) -> None:
        """Build the SQLAlchemy engine and create tables.

        Pool sizing is controlled by ``DB_POOL_SIZE`` and
        ``DB_POOL_MAX_OVERFLOW`` so Kubernetes deployments can keep total
        Postgres connection budgets below the server cap.

        pgvector bootstrap and the embedding tables are created only when
        ``settings.embeddings_enabled`` is true *and* the pgvector extension is
        available. If embeddings are disabled or the extension can't be created,
        the rest of the schema is bootstrapped without the ``Vector`` columns so
        a plain PostgreSQL without pgvector still boots (vector search stays off).

        Args:
            db_url: PostgreSQL DSN to connect to.
        """
        self.vector_search_enabled = False
        # Set after the schema exists: BM25 lexical ranking via pg_search when
        # available, else explore search uses ILIKE substring matching.
        self.bm25_search_enabled = False
        self._max_progress_events = settings.progress_events_per_job_cap
        self._code_version = settings.code_version
        self._progress_event_counters: defaultdict[str, int] = defaultdict(int)
        self._progress_counter_lock = threading.Lock()
        self._log_quota = LogQuotaGate(
            lambda username: self.compute_user_storage(username).total, self.get_effective_user_storage_quota
        )
        # Force every connection's session timezone to UTC so TZ-aware writes
        # into TIMESTAMPTZ columns round-trip without offset rotation, and any
        # naive value that slipped into legacy rows is interpreted as UTC.
        self._engine = create_engine(
            db_url,
            **_build_engine_kwargs(db_url),
        )
        if settings.embeddings_enabled and settings.embeddings_dim != EMBEDDING_DIM:
            # The embedding columns are a fixed-width vector(EMBEDDING_DIM). A
            # mismatched EMBEDDINGS_DIM boots fine but pgvector then rejects every
            # mismatched-length insert on the daemon embed thread (warn-and-drop),
            # so explore search silently degrades to lexical. Fail loudly instead.
            raise RuntimeError(
                f"EMBEDDINGS_DIM={settings.embeddings_dim} does not match the "
                f"vector({EMBEDDING_DIM}) schema column; embedding writes would be "
                f"silently rejected. Set EMBEDDINGS_DIM={EMBEDDING_DIM}, disable "
                "embeddings, or run a migration to change the column width."
            )
        # Migrations first, so a migration that creates or renames a table doesn't
        # collide with the empty table create_all would make from the new models.
        upgrade_if_adopted(self._engine)
        if settings.embeddings_enabled and self._bootstrap_pgvector():
            with schema_bootstrap_lock(self._engine) as conn:
                Base.metadata.create_all(conn if conn is not None else self._engine)
            self.vector_search_enabled = self._bootstrap_vector_indexes()
        else:
            # Embeddings disabled, or pgvector is unavailable on this database
            # (managed/plain Postgres without CREATE EXTENSION privilege). Create
            # the full schema minus the Vector(512) tables so the app still boots;
            # otherwise CREATE TABLE job_embeddings raises UndefinedObject and
            # propagates unguarded out of create_app(). Vector search stays off.
            embedding_tables = {
                JobEmbeddingModel.__table__,
                ConversationEmbeddingModel.__table__,
            }
            non_embedding_tables = [table for table in Base.metadata.sorted_tables if table not in embedding_tables]
            with schema_bootstrap_lock(self._engine) as conn:
                Base.metadata.create_all(
                    conn if conn is not None else self._engine,
                    tables=non_embedding_tables,
                )
        # A database Alembic never tracked now holds the head schema; adopt it.
        stamp_if_unadopted(self._engine)
        # Lexical search ranking. Independent of pgvector/embeddings: BM25
        # serves the default (embeddings-off) explore search when pg_search is
        # installed, otherwise the ILIKE fallback handles it.
        self.bm25_search_enabled = settings.search_bm25_enabled and self._bootstrap_bm25()
        self._session_factory = sessionmaker(bind=self._engine)
        self._checkpoints = PostgresCheckpointBlobStore(self._engine)
        self._grid_pair_results = PostgresGridPairResultStore(self._engine)
        self._agent_runs = PostgresAgentRunStore(self._engine)
        self._run_calls = PostgresRunCallStore(self._engine)
        parsed_url = urlparse(db_url)
        db_location = parsed_url.hostname or "<masked>"
        if parsed_url.port:
            db_location = f"{db_location}:{parsed_url.port}"
        logger.info("Initialized PostgreSQL database at %s", db_location)

    @property
    def _progress_events_cap(self) -> int:
        """Return the per-job progress-event retention cap."""
        return getattr(self, "_max_progress_events", MAX_PROGRESS_EVENTS)

    @property
    def _current_code_version(self) -> str:
        """Return the cached worker code version for this store."""
        return getattr(self, "_code_version", settings.code_version)

    def _bootstrap_pgvector(self) -> bool:
        """Ensure the pgvector extension exists before creating Vector columns.

        Safe to call repeatedly. If the database role lacks the privilege
        to install the extension, the log line below is the operator's
        hint to run ``CREATE EXTENSION vector`` once out-of-band.
        """
        try:
            with self._engine.connect() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.commit()
            return True
        except SQLAlchemyError as exc:
            logger.warning(
                "pgvector extension bootstrap failed (%s). "
                "Recommendation service will be unavailable until an admin runs "
                "'CREATE EXTENSION vector' on the database.",
                exc,
            )
            return False

    def _bootstrap_vector_indexes(self) -> bool:
        """Create HNSW cosine indexes on the job_embeddings vector columns.

        SQLAlchemy's create_all can't express HNSW, and we don't want to
        pay for a reindex on every restart — the IF NOT EXISTS guard
        makes the call free on warm databases.
        """
        statements = [
            (
                "CREATE INDEX IF NOT EXISTS idx_job_embeddings_summary_hnsw "
                "ON job_embeddings USING hnsw (embedding_summary vector_cosine_ops)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_job_embeddings_code_hnsw "
                "ON job_embeddings USING hnsw (embedding_code vector_cosine_ops)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_job_embeddings_schema_hnsw "
                "ON job_embeddings USING hnsw (embedding_schema vector_cosine_ops)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_conversation_embeddings_summary_hnsw "
                "ON conversation_embeddings USING hnsw (embedding_summary vector_cosine_ops)"
            ),
        ]
        try:
            with self._engine.connect() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
                conn.commit()
            return True
        except SQLAlchemyError as exc:
            logger.warning("HNSW index bootstrap skipped: %s", exc)
            return False

    def _bootstrap_bm25(self) -> bool:
        """Best-effort: enable BM25 lexical ranking via the pg_search extension.

        Creates the ``pg_search`` extension and a BM25 index over the ``jobs``
        ``payload_overview`` corpus (task name/description/optimizer/model/module
        — the text that exists when embeddings are off) so explore search ranks
        lexically with real relevance scores. Entirely optional: when pg_search
        is absent (the common case on a plain/managed Postgres without it) or the
        role can't create it, this logs and returns False and search falls back
        to ILIKE substring matching. Safe to call repeatedly; the
        ``IF NOT EXISTS`` guards make it free on warm databases.

        Returns:
            True when pg_search and the BM25 index are in place, else False.
        """
        try:
            with self._engine.connect() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_search"))
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_jobs_bm25 ON jobs "
                        "USING bm25 (optimization_id, payload_overview) "
                        "WITH (key_field='optimization_id')"
                    )
                )
                conn.commit()
            logger.info("BM25 lexical search enabled (pg_search).")
            return True
        except SQLAlchemyError as exc:
            logger.info(
                "BM25 search unavailable (%s); explore search uses ILIKE lexical "
                "matching. Install the pg_search extension to enable BM25 ranking.",
                exc,
            )
            return False

    @property
    def engine(self) -> Engine:
        """Return the SQLAlchemy engine backing this store.

        Exposed so callers can run shared table operations (direct
        DDL, joined queries) outside the ORM session factory.

        Returns:
            The configured SQLAlchemy engine.
        """
        return self._engine

    def _get_session(self) -> Session:
        """Create and return a new SQLAlchemy session.

        Returns:
            A new session; the caller is responsible for closing it.
        """
        return self._session_factory()

    def _job_to_dict(self, job: JobModel, *, include_payload: bool = True, include_result: bool = True) -> JobRecord:
        """Convert a JobModel ORM instance to its TypedDict representation.

        Args:
            job: SQLAlchemy ORM row to serialize.
            include_payload: When ``False``, the (potentially multi-MB) ``payload``
                JSONB is reported as ``None`` instead of being read off the row.
                List/SSE paths pass ``False`` so the column is never materialized;
                callers that defer it on the query (see :meth:`_list_query`)
                avoid the DB read entirely.
            include_result: When ``False``, ``result`` is likewise reported as
                ``None`` instead of being read; the list paths defer the column
                and splice in the pruned summary from
                :func:`_assemble_result_summary` instead.

        Returns:
            A ``JobRecord`` with ISO-formatted timestamps and JSON columns
            normalized to plain dicts.
        """
        return cast(
            JobRecord,
            {
                "optimization_id": job.optimization_id,
                "status": job.status,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                "estimated_remaining_seconds": job.estimated_remaining_seconds,
                "message": job.message,
                "latest_metrics": job.latest_metrics or {},
                "result": job.result if include_result else None,
                "payload_overview": job.payload_overview or {},
                "payload": job.payload if include_payload else None,
                "username": job.username,
                "optimization_type": job.optimization_type,
                "attempts": job.attempts,
                "execution_generation": job.execution_generation,
                "execution_budget_id": job.execution_budget_id,
                "execution_budget_generation": job.execution_budget_generation,
                "stop_reason": job.stop_reason,
                "result_availability": job.result_availability,
                "terminal_evidence": job.terminal_evidence,
                "recovery": job.recovery,
                "code_version": job.code_version,
                "sandbox_image": job.sandbox_image,
                "stored_bytes": job.stored_bytes or 0,
                "accumulated_runtime_seconds": job.accumulated_runtime_seconds or 0.0,
                "parent_optimization_id": job.parent_optimization_id,
                "pair_index": job.pair_index,
            },
        )

    def create_job(
        self,
        optimization_id: str,
        estimated_remaining_seconds: float | None = None,
        *,
        username: str | None = None,
        idempotency_key: str | None = None,
        execution_budget_id: str | None = None,
        execution_budget_revision: int | None = None,
        budget_service: Any = None,
        preflight_id: str | None = None,
        preflight_fingerprint: str | None = None,
        preflight_payload: dict[str, Any] | None = None,
        preflight_workflow: str | None = None,
        preflight_store: Any = None,
    ) -> JobRecord:
        """Create a new job record in the database.

        Args:
            optimization_id: Unique identifier for the new job.
            estimated_remaining_seconds: Initial ETA, or ``None`` if unknown.
            username: Submitter recorded on the row up-front so the partial
                unique index on ``(username, idempotency_key)`` can guard
                against duplicate POSTs before ``set_payload_overview`` runs.
            idempotency_key: Optional client-supplied dedup key; pairs with
                ``username`` for the uniqueness check.
            execution_budget_id: Setup/run budget to attach atomically.
            execution_budget_revision: Revision the user authorized for submission.
            budget_service: Injected authoritative budget service, avoiding a storage/billing import cycle.
            preflight_id: Completed execution-check identity.
            preflight_fingerprint: Configuration digest approved by those checks.
            preflight_payload: Canonical current request before runtime mutations.
            preflight_workflow: Exact request family.
            preflight_store: Injected verification authority sharing the attachment transaction.

        Returns:
            The newly inserted row as a ``JobRecord``.
        """
        now = datetime.now(UTC)
        job = JobModel(
            optimization_id=optimization_id,
            status="pending",
            created_at=now,
            estimated_remaining_seconds=estimated_remaining_seconds,
            latest_metrics={},
            payload_overview={},
            attempts=0,
            code_version=self._current_code_version,
            username=username,
            idempotency_key=idempotency_key,
        )
        session = self._get_session()
        try:
            session.add(job)
            if execution_budget_id is not None:
                if budget_service is None or username is None or execution_budget_revision is None:
                    raise ValueError("Budget attachment requires an owner, revision, and budget service.")
                if preflight_store is None or preflight_payload is None or preflight_workflow is None:
                    raise ValueError("Protected attachment requires current setup verification.")
                preflight_store.require_current(
                    username=username,
                    budget_id=execution_budget_id,
                    identity=preflight_id,
                    fingerprint=preflight_fingerprint,
                    workflow=preflight_workflow,
                    payload=preflight_payload,
                    session=session,
                )
                budget = budget_service.attach_to_job(
                    execution_budget_id,
                    username,
                    optimization_id,
                    expected_revision=execution_budget_revision,
                    session=session,
                )
                job.execution_budget_id = budget.id
                job.execution_budget_generation = budget.generation
            session.commit()
            session.refresh(job)
            return self._job_to_dict(job)
        finally:
            session.close()

    def find_job_by_idempotency_key(self, username: str, idempotency_key: str) -> str | None:
        """Return the ``optimization_id`` previously submitted under this key.

        Args:
            username: Submitter to scope the lookup to.
            idempotency_key: Client-supplied dedup key from the request header.

        Returns:
            The matching job id, or ``None`` when no prior submission used the key.
        """
        if not username or not idempotency_key:
            return None
        session = self._get_session()
        try:
            row = (
                session.query(JobModel.optimization_id)
                .filter(JobModel.username == username)
                .filter(JobModel.idempotency_key == idempotency_key)
                .first()
            )
            return row[0] if row else None
        finally:
            session.close()

    def claim_completion_notification(self, optimization_id: str) -> bool:
        """Atomically claim the right to send the completion notification.

        Multiple paths can converge on a single job's completion: the worker
        that finished it, a peer that orphan-recovered it after a lease
        expiry, and the cancellation handler. Each one calls
        ``notify_job_completed`` which would otherwise re-send the message
        once per attempt. This method does a single ``UPDATE ... WHERE
        notified_at IS NULL`` and reports whether the caller won the CAS;
        only the winner should send the notification.

        Args:
            optimization_id: ID of the job whose notification is being sent.

        Returns:
            ``True`` if the caller won the race and should send the message,
            ``False`` when a prior attempt already notified (or the job is
            missing — there is nothing to notify about).
        """
        now = datetime.now(UTC)
        session = self._get_session()
        try:
            rows = (
                session.query(JobModel)
                .filter(JobModel.optimization_id == optimization_id)
                .filter(JobModel.notified_at.is_(None))
                .update({JobModel.notified_at: now}, synchronize_session=False)
            )
            session.commit()
            return rows > 0
        finally:
            session.close()

    def claim_ended_orphan_notification(self, optimization_id: str, generation: int) -> JobRecord | None:
        """Claim the notification of a run that a sweep ended and read it in the same transaction.

        The read happens before the commit, while the claim's row lock is
        held, so an owner resume cannot change the run between the claim and
        the snapshot that the notification describes.

        Args:
            optimization_id: ID of the run that the sweep failed or stopped.
            generation: Execution generation that the sweep ended.

        Returns:
            The run as it was when the claim matched, without its payload, or
            ``None`` when a prior attempt already notified, the run left the
            ``failed``/``stopped`` state or the generation, or it is missing.
        """
        session = self._get_session()
        try:
            rows = (
                session.query(JobModel)
                .filter(JobModel.optimization_id == optimization_id)
                .filter(JobModel.notified_at.is_(None))
                .filter(JobModel.status.in_(("failed", "stopped")))
                .filter(JobModel.execution_generation == generation)
                .update({JobModel.notified_at: datetime.now(UTC)}, synchronize_session=False)
            )
            if rows == 0:
                session.rollback()
                return None
            job = (
                session.query(JobModel)
                .options(defer(JobModel.payload))
                .filter(JobModel.optimization_id == optimization_id)
                .one()
            )
            snapshot = self._job_to_dict(job, include_payload=False)
            session.commit()
            return snapshot
        finally:
            session.close()

    def claim_job_notification(
        self,
        optimization_id: str,
        claim: str,
        *,
        cap_prefix: str | None = None,
        cap: int | None = None,
    ) -> bool:
        """Atomically claim the right to send one in-run email for a job.

        The in-run counterpart of :meth:`claim_completion_notification`: each
        email kind (``new_best``, ``stage:baseline``, ``digest:<window>`` ...)
        is recorded on the job row under a row lock, so a resumed run, an
        orphan-recovered run or parallel grid pairs all writing to one parent
        send it once. ``cap_prefix``/``cap`` bound how many claims sharing a
        prefix a run may hold (the per-run progress-email cap).

        Args:
            optimization_id: Job the email is about.
            claim: Stable identifier of the email kind for this run.
            cap_prefix: Prefix of the claims counted against ``cap``.
            cap: Maximum number of claims starting with ``cap_prefix``.

        Returns:
            ``True`` when the caller won the claim and should send the email,
            ``False`` when it was already claimed, the cap is reached, or the
            job is missing.
        """
        session = self._get_session()
        try:
            job = (
                session.query(JobModel)
                .filter(JobModel.optimization_id == optimization_id)
                .with_for_update()
                .one_or_none()
            )
            if job is None:
                return False
            claims = list(job.notification_claims or [])
            if claim in claims:
                return False
            if (
                cap_prefix is not None
                and cap is not None
                and sum(1 for existing in claims if existing.startswith(cap_prefix)) >= cap
            ):
                return False
            job.notification_claims = [*claims, claim]  # type: ignore[assignment]
            session.commit()
            return True
        finally:
            session.close()

    def update_job(self, optimization_id: str, **kwargs: Any) -> None:
        """Update fields on an existing job.

        Datetime string values are automatically parsed from ISO format;
        ``latest_metrics`` is merged into the existing mapping rather
        than replacing it. The write is a non-locking ``UPDATE`` —
        ``SELECT ... FOR UPDATE`` would serialise concurrent writers on
        this row, which becomes a contention hotspot at scale and is
        unnecessary because each optimization has a single worker writer
        in flight (lifecycle cancellation is the only realistic peer and
        last-writer-wins is acceptable for ``status``/``message``).

        Args:
            optimization_id: ID of the job to update.
            **kwargs: Column values to overwrite.

        Raises:
            KeyError: When the job does not exist.
            ValueError: When ``kwargs`` contains a column name absent from ``JobModel``.
        """
        datetime_fields = {"created_at", "started_at", "completed_at"}
        mutable_columns = set(JobModel.__table__.columns.keys()) - _IMMUTABLE_JOB_COLUMNS
        invalid_fields = sorted(set(kwargs) - mutable_columns)
        if invalid_fields:
            raise ValueError(f"Unknown field '{invalid_fields[0]}' on JobModel")

        merging_metrics = "latest_metrics" in kwargs
        recompute_stored = bool(set(_STORED_BYTES_JSON_COLUMNS) & set(kwargs))
        update_values: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in datetime_fields and isinstance(value, str):
                value = datetime.fromisoformat(value)
            update_values[key] = value

        session = self._get_session()
        try:
            if merging_metrics:
                existing = (
                    session.query(JobModel.latest_metrics).filter(JobModel.optimization_id == optimization_id).first()
                )
                if existing is None:
                    raise KeyError(f"Job '{optimization_id}' not found")
                current_metrics = existing[0] or {}
                merged = dict(current_metrics)
                merged.update(update_values["latest_metrics"])
                update_values["latest_metrics"] = merged

            if recompute_stored:
                stored_row = (
                    session.query(*_STORED_BYTES_READ_COLUMNS)
                    .filter(JobModel.optimization_id == optimization_id)
                    .first()
                )
                if stored_row is None:
                    raise KeyError(f"Job '{optimization_id}' not found")
                update_values["stored_bytes"] = _recomputed_stored_bytes(stored_row, update_values)

            rows = (
                session.query(JobModel)
                .filter(JobModel.optimization_id == optimization_id)
                .update(update_values, synchronize_session=False)
            )
            if rows == 0:
                raise KeyError(f"Job '{optimization_id}' not found")
            session.commit()
        finally:
            session.close()

    def update_job_if_status(
        self, optimization_id: str, expected: tuple[str, ...], *, expected_generation: int | None = None, **kwargs: Any
    ) -> bool:
        """Update a job only while its status is one of ``expected`` (compare-and-set).

        The conditional ``WHERE status IN (...)`` closes the last-writer-wins
        race on the worker-completion and pause/cancel paths: a terminal write
        can't clobber a status another writer already moved the row to. Datetime
        strings are parsed and ``stored_bytes`` is recomputed exactly as
        :meth:`update_job` does; ``latest_metrics`` merging is not supported.

        Args:
            optimization_id: ID of the job to update.
            expected: Status values the row must currently hold for the write to
                apply.
            expected_generation: Execution fence required for worker-owned writes.
            **kwargs: Column values to overwrite.

        Returns:
            ``True`` when the row matched and was updated; ``False`` when the
            status no longer matched (the caller should treat the write as lost).

        Raises:
            ValueError: When ``kwargs`` names a column absent from ``JobModel``.
        """
        mutable_columns = set(JobModel.__table__.columns.keys()) - _IMMUTABLE_JOB_COLUMNS
        invalid_fields = sorted(set(kwargs) - mutable_columns)
        if invalid_fields:
            raise ValueError(f"Unknown field '{invalid_fields[0]}' on JobModel")
        datetime_fields = {"created_at", "started_at", "completed_at"}
        update_values: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in datetime_fields and isinstance(value, str):
                value = datetime.fromisoformat(value)
            update_values[key] = value

        session = self._get_session()
        try:
            if set(_STORED_BYTES_JSON_COLUMNS) & set(update_values):
                stored_row = (
                    session.query(*_STORED_BYTES_READ_COLUMNS)
                    .filter(JobModel.optimization_id == optimization_id)
                    .first()
                )
                if stored_row is not None:
                    update_values["stored_bytes"] = _recomputed_stored_bytes(stored_row, update_values)
            query = (
                session.query(JobModel)
                .filter(JobModel.optimization_id == optimization_id)
                .filter(JobModel.status.in_(expected))
            )
            if expected_generation is not None:
                query = query.filter(JobModel.execution_generation == expected_generation)
            rows = query.update(update_values, synchronize_session=False)
            session.commit()
            return rows > 0
        finally:
            session.close()

    def get_job(self, optimization_id: str, *, include_payload: bool = True) -> JobRecord:
        """Retrieve a job by its ID.

        Args:
            optimization_id: ID of the job to fetch.
            include_payload: When ``False``, the (potentially multi-MB)
                ``payload`` JSONB is deferred on the query and reported as
                ``None`` — hot polling paths that never read the training
                dataset pass ``False`` to skip transferring it.

        Returns:
            The matching ``JobRecord``.

        Raises:
            KeyError: When the job does not exist.
        """
        session = self._get_session()
        try:
            q = session.query(JobModel)
            if not include_payload:
                q = q.options(defer(JobModel.payload))
            job = q.filter(JobModel.optimization_id == optimization_id).first()
            if not job:
                raise KeyError(f"Job '{optimization_id}' not found")
            return self._job_to_dict(job, include_payload=include_payload)
        finally:
            session.close()

    def get_job_status_fields(self, optimization_id: str) -> JobRecord:
        """Retrieve only the live-polling fields for a job.

        Selects the status columns plus the budget id and owner directly so the
        per-job SSE loop can poll every few seconds without re-reading the
        ``payload`` JSONB that :meth:`get_job` materializes.

        Args:
            optimization_id: ID of the job to read.

        Returns:
            A partial ``JobRecord`` with ``status``, ``message`` and
            ``latest_metrics``.

        Raises:
            KeyError: When the job does not exist.
        """
        session = self._get_session()
        try:
            row = (
                session.query(
                    JobModel.status,
                    JobModel.message,
                    JobModel.latest_metrics,
                    JobModel.recovery,
                    JobModel.terminal_evidence,
                    JobModel.stop_reason,
                    JobModel.result_availability,
                    JobModel.execution_budget_id,
                    JobModel.username,
                )
                .filter(JobModel.optimization_id == optimization_id)
                .first()
            )
            if row is None:
                raise KeyError(f"Job '{optimization_id}' not found")
            return cast(
                JobRecord,
                {
                    "status": row[0],
                    "message": row[1],
                    "latest_metrics": row[2] or {},
                    "recovery": row[3],
                    "terminal_evidence": row[4],
                    "stop_reason": row[5],
                    "result_availability": row[6],
                    "execution_budget_id": row[7],
                    "username": row[8],
                },
            )
        finally:
            session.close()

    def job_exists(self, optimization_id: str) -> bool:
        """Return ``True`` if the job exists in the database.

        Args:
            optimization_id: ID to check.

        Returns:
            Whether the row is present.
        """
        session = self._get_session()
        try:
            return (
                session.query(JobModel.optimization_id).filter(JobModel.optimization_id == optimization_id).first()
                is not None
            )
        finally:
            session.close()

    def delete_job(self, optimization_id: str) -> None:
        """Delete a job and all its associated logs and progress events.

        Missing IDs are a silent no-op.

        Args:
            optimization_id: ID of the job to remove.
        """
        session = self._get_session()
        try:
            session.query(LogEntryModel).filter(LogEntryModel.optimization_id == optimization_id).delete()
            session.query(ProgressEventModel).filter(ProgressEventModel.optimization_id == optimization_id).delete()
            if settings.embeddings_enabled:
                session.query(JobEmbeddingModel).filter(JobEmbeddingModel.optimization_id == optimization_id).delete()
            session.query(GepaCheckpointModel).filter(GepaCheckpointModel.optimization_id == optimization_id).delete()
            session.query(GridPairResultModel).filter(GridPairResultModel.optimization_id == optimization_id).delete()
            session.query(BlackboxAgentRunModel).filter(
                BlackboxAgentRunModel.optimization_id == optimization_id
            ).delete()
            session.query(RunCallModel).filter(RunCallModel.optimization_id == optimization_id).delete()
            session.query(RunCandidateModel).filter(RunCandidateModel.optimization_id == optimization_id).delete()
            session.query(RunFolderItemModel).filter(RunFolderItemModel.optimization_id == optimization_id).delete()
            self._close_deleted_job_budgets(session, [optimization_id])
            self._delete_grid_pair_children(session, optimization_id)
            session.query(JobModel).filter(JobModel.optimization_id == optimization_id).delete()
            session.commit()
        finally:
            session.close()
        self._evict_job_counters([optimization_id])

    def _close_deleted_job_budgets(self, session: Session, optimization_ids: list[str]) -> None:
        """Close the execution budgets of jobs (and their grid pairs) about to be deleted.

        Budgets and ledger rows are financial history with no foreign key to
        ``jobs``, so they are kept; this releases unstarted holds and drops the
        link that would otherwise point at a deleted job.

        Args:
            session: The open delete session the caller commits.
            optimization_ids: Root job ids being deleted.
        """
        child_ids = [
            row[0]
            for row in session.query(JobModel.optimization_id)
            .filter(JobModel.parent_optimization_id.in_(optimization_ids))
            .all()
        ]
        BudgetService(engine=self._engine).close_for_deleted_jobs([*optimization_ids, *child_ids], session=session)

    @staticmethod
    def _delete_grid_pair_children(session: Session, parent_optimization_id: str) -> None:
        """Delete a grid parent's pair-child rows and their per-child stores.

        Explicit (rather than relying on the ``ON DELETE CASCADE`` self-FK)
        so SQLite test runs without foreign-key enforcement clean up exactly
        like Postgres. Child logs/progress need no handling — a pair child
        writes those onto its PARENT's id.

        Args:
            session: The open session the caller commits.
            parent_optimization_id: The grid parent whose children go away.
        """
        child_ids = [
            row[0]
            for row in session.query(JobModel.optimization_id)
            .filter(JobModel.parent_optimization_id == parent_optimization_id)
            .all()
        ]
        if not child_ids:
            return
        session.query(GepaCheckpointModel).filter(GepaCheckpointModel.optimization_id.in_(child_ids)).delete(
            synchronize_session=False
        )
        session.query(GridPairResultModel).filter(GridPairResultModel.optimization_id.in_(child_ids)).delete(
            synchronize_session=False
        )
        session.query(BlackboxAgentRunModel).filter(BlackboxAgentRunModel.optimization_id.in_(child_ids)).delete(
            synchronize_session=False
        )
        session.query(RunCallModel).filter(RunCallModel.optimization_id.in_(child_ids)).delete(
            synchronize_session=False
        )
        session.query(RunCandidateModel).filter(RunCandidateModel.optimization_id.in_(child_ids)).delete(
            synchronize_session=False
        )
        session.query(JobModel).filter(JobModel.optimization_id.in_(child_ids)).delete(synchronize_session=False)

    def _evict_job_counters(self, optimization_ids: list[str]) -> None:
        """Drop the per-job in-memory bookkeeping for deleted jobs.

        The sticky-tqdm map and the sampled trim counters are keyed by
        optimization id and otherwise live for the process lifetime — without
        eviction a long-lived store grows one entry per job ever processed.

        Args:
            optimization_ids: IDs whose in-memory entries should be dropped.
        """
        if hasattr(self, "_tqdm_sticky"):
            with self._tqdm_sticky_lock:
                for oid in optimization_ids:
                    self._tqdm_sticky.pop(oid, None)
        if hasattr(self, "_progress_event_counters"):
            with self._progress_counter_lock:
                for oid in optimization_ids:
                    self._progress_event_counters.pop(oid, None)
        if hasattr(self, "_log_append_counters"):
            with self._log_counter_lock:
                for oid in optimization_ids:
                    self._log_append_counters.pop(oid, None)

    def get_jobs_status_by_ids(self, optimization_ids: list[str]) -> dict[str, str]:
        """Return a ``{id: status}`` map for the requested IDs.

        Runs a single ``SELECT ... WHERE optimization_id IN (...)``
        so batch existence + status checks cost one round trip
        regardless of how many IDs are supplied.

        Args:
            optimization_ids: IDs to look up.

        Returns:
            Mapping of present IDs to their status strings; missing IDs
            are simply absent from the result.
        """
        if not optimization_ids:
            return {}
        session = self._get_session()
        try:
            rows = (
                session.query(JobModel.optimization_id, JobModel.status)
                .filter(JobModel.optimization_id.in_(optimization_ids))
                .all()
            )
            return {r[0]: r[1] for r in rows}
        finally:
            session.close()

    def delete_jobs(self, optimization_ids: list[str]) -> int:
        """Hard-delete a batch of jobs in a single transaction.

        Drops the associated log, progress-event, embedding and checkpoint rows
        first then commits once, so the round-trip cost is bounded regardless of
        batch size.

        Args:
            optimization_ids: IDs to remove. Duplicates and missing IDs are tolerated.

        Returns:
            The number of job rows actually deleted.
        """
        if not optimization_ids:
            return 0
        session = self._get_session()
        try:
            session.query(LogEntryModel).filter(LogEntryModel.optimization_id.in_(optimization_ids)).delete(
                synchronize_session=False
            )
            session.query(ProgressEventModel).filter(ProgressEventModel.optimization_id.in_(optimization_ids)).delete(
                synchronize_session=False
            )
            if settings.embeddings_enabled:
                session.query(JobEmbeddingModel).filter(JobEmbeddingModel.optimization_id.in_(optimization_ids)).delete(
                    synchronize_session=False
                )
            session.query(GepaCheckpointModel).filter(GepaCheckpointModel.optimization_id.in_(optimization_ids)).delete(
                synchronize_session=False
            )
            session.query(GridPairResultModel).filter(GridPairResultModel.optimization_id.in_(optimization_ids)).delete(
                synchronize_session=False
            )
            session.query(BlackboxAgentRunModel).filter(
                BlackboxAgentRunModel.optimization_id.in_(optimization_ids)
            ).delete(synchronize_session=False)
            session.query(RunCallModel).filter(RunCallModel.optimization_id.in_(optimization_ids)).delete(
                synchronize_session=False
            )
            session.query(RunCandidateModel).filter(RunCandidateModel.optimization_id.in_(optimization_ids)).delete(
                synchronize_session=False
            )
            session.query(RunFolderItemModel).filter(RunFolderItemModel.optimization_id.in_(optimization_ids)).delete(
                synchronize_session=False
            )
            self._close_deleted_job_budgets(session, optimization_ids)
            for parent_id in optimization_ids:
                self._delete_grid_pair_children(session, parent_id)
            deleted = (
                session.query(JobModel)
                .filter(JobModel.optimization_id.in_(optimization_ids))
                .delete(synchronize_session=False)
            )
            session.commit()
        finally:
            session.close()
        self._evict_job_counters(optimization_ids)
        return int(deleted or 0)

    def save_agent_run(self, optimization_id: str, run: dict[str, Any]) -> None:
        """Persist (or replace) one sandboxed agent run's record.

        Args:
            optimization_id: Owning job id.
            run: The record as the job's recorder sends it, keyed by ``run_id``.
        """
        self._agent_runs.save(optimization_id, run)

    def append_agent_run_transcript(self, optimization_id: str, run_id: int, text: str) -> None:
        """Add a piece of live transcript to a sandboxed agent run.

        Args:
            optimization_id: Owning job id.
            run_id: The run's ordinal within the job.
            text: The new transcript text.
        """
        self._agent_runs.append_transcript(optimization_id, run_id, text)

    def get_agent_run(self, optimization_id: str, run_id: int) -> dict[str, Any] | None:
        """Return one sandboxed agent run's record, or ``None``.

        Args:
            optimization_id: Owning job id.
            run_id: The run's ordinal within the job.

        Returns:
            The record, or ``None`` when no row exists.
        """
        return self._agent_runs.get(optimization_id, run_id)

    def record_run_call(self, optimization_id: str, call: dict[str, Any], secrets: list[str]) -> int:
        """Write one relay call of a run, with its secrets removed and its bodies cut.

        Args:
            optimization_id: Owning job id.
            call: The call record the relay builds.
            secrets: Every secret value the call could carry.

        Returns:
            The new call's ``seq``, which is also its cursor.
        """
        return self._run_calls.record_call(optimization_id, call, secrets)

    def set_run_candidate_state(self, optimization_id: str, candidate_id: str, role: str, state: str) -> None:
        """Insert or update one candidate's role and lifecycle state.

        Args:
            optimization_id: Owning job id.
            candidate_id: The candidate within the run.
            role: The candidate's role or stage.
            state: ``running``, ``done`` or ``failed``.
        """
        self._run_calls.set_candidate_state(optimization_id, candidate_id, role, state)

    def list_run_calls(
        self, optimization_id: str, candidate_id: str, *, since: int = 0, limit: int = 100
    ) -> list[dict]:
        """Return a candidate's relay calls after the ``since`` cursor, oldest first.

        Args:
            optimization_id: Owning job id.
            candidate_id: The candidate within the run.
            since: The last call ``seq`` the caller holds.
            limit: The most calls to return.

        Returns:
            The call records.
        """
        return self._run_calls.list_calls(optimization_id, candidate_id, since=since, limit=limit)

    def get_run_candidate(self, optimization_id: str, candidate_id: str) -> dict | None:
        """Return one candidate's summary, or ``None`` when the run has no such candidate.

        Args:
            optimization_id: Owning job id.
            candidate_id: The candidate within the run.

        Returns:
            The candidate summary, or ``None``.
        """
        return self._run_calls.get_candidate(optimization_id, candidate_id)

    def list_run_candidates(self, optimization_id: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
        """Return one page of the run's candidates with role, state, call count and cost so far.

        Args:
            optimization_id: Owning job id.
            limit: The most candidates to return.
            offset: The number of candidates to skip, in start order.

        Returns:
            One summary for each candidate on the page.
        """
        return self._run_calls.list_candidates(optimization_id, limit=limit, offset=offset)

    def save_gepa_checkpoint(
        self,
        optimization_id: str,
        data: bytes,
        iteration: int,
        pair_index: int = -1,
        *,
        manifest: dict[str, Any] | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """Persist (or replace) the latest GEPA state blob for one run or grid pair.

        Args:
            optimization_id: Owning job id.
            data: Raw ``gepa_state.bin`` bytes for the latest iteration.
            iteration: The iteration index the state was saved at.
            pair_index: Grid pair index, or ``-1`` for a single run.
            manifest: Exact engine and task evidence for compatible restoration.
            expected_generation: Current worker epoch authorizing publication.

        Returns:
            Whether the checkpoint was accepted.
        """
        return self._checkpoints.put(
            optimization_id,
            data=data,
            iteration=iteration,
            pair_index=pair_index,
            manifest=manifest,
            expected_generation=expected_generation,
        )

    def get_gepa_checkpoint(self, optimization_id: str, pair_index: int = -1) -> GepaCheckpoint | None:
        """Return the saved GEPA checkpoint for one run/pair, or ``None``.

        Args:
            optimization_id: Job whose checkpoint is read.
            pair_index: Grid pair index, or ``-1`` for a single run.

        Returns:
            The :class:`GepaCheckpoint` (state bytes plus iteration), or ``None``.
        """
        return self._checkpoints.get(optimization_id, pair_index)

    def list_gepa_checkpoints(self, optimization_id: str) -> list[GepaCheckpoint]:
        """Return every saved checkpoint for a job (all grid pairs, or the single run).

        Args:
            optimization_id: Job whose checkpoints are read.

        Returns:
            The job's :class:`GepaCheckpoint` rows (possibly empty).
        """
        return self._checkpoints.list_for_optimization(optimization_id)

    def delete_gepa_checkpoint(self, optimization_id: str, pair_index: int = -1) -> None:
        """Drop one run/pair's GEPA checkpoint (no-op when absent).

        Args:
            optimization_id: Owning job id.
            pair_index: Grid pair index, or ``-1`` for a single run.
        """
        self._checkpoints.delete(optimization_id, pair_index)

    def delete_all_gepa_checkpoints(self, optimization_id: str) -> None:
        """Drop every checkpoint for a job (e.g. once a grid succeeds).

        Args:
            optimization_id: Job whose checkpoints are freed.
        """
        self._checkpoints.delete_all(optimization_id)

    def has_gepa_checkpoint(self, optimization_id: str) -> bool:
        """Return whether any resumable GEPA checkpoint exists for ``optimization_id``.

        Cheap key-only existence check (single run or any grid pair) used to gate
        the per-job ``resumable`` flag on the list/detail read paths.

        Args:
            optimization_id: Job to test.

        Returns:
            ``True`` when at least one checkpoint row exists.
        """
        return self._checkpoints.has_any(optimization_id)

    def save_grid_pair_result(
        self, optimization_id: str, pair_index: int, result: dict[str, Any], *, expected_generation: int | None = None
    ) -> bool:
        """Persist (or replace) one completed grid pair's result so resume can skip it.

        Args:
            optimization_id: Owning grid job id.
            pair_index: The completed pair's index.
            result: The pair's serialized ``PairResult``.
            expected_generation: Worker epoch authorizing this pair publication.

        Returns:
            Whether the current generation stored the result.
        """
        return self._grid_pair_results.put(optimization_id, pair_index, result, expected_generation=expected_generation)

    def get_grid_pair_results(self, optimization_id: str) -> dict[int, dict[str, Any]]:
        """Return ``{pair_index: result}`` for every completed pair of a grid.

        Args:
            optimization_id: Grid job whose finished pairs are read.

        Returns:
            Mapping of completed pair index to its serialized result (possibly empty).
        """
        return self._grid_pair_results.get_all(optimization_id)

    def delete_grid_pair_results(self, optimization_id: str) -> None:
        """Drop every stored pair result for a grid (e.g. once it succeeds).

        Args:
            optimization_id: Grid job whose pair results are freed.
        """
        self._grid_pair_results.delete_all(optimization_id)

    def delete_grid_pair_result(self, optimization_id: str, pair_index: int) -> None:
        """Drop one pair's stored result (targeted per-pair re-run).

        Args:
            optimization_id: Grid job owning the pair.
            pair_index: The pair whose stored result is dropped.
        """
        self._grid_pair_results.delete_one(optimization_id, pair_index)

    def list_finalizable_grid_parents(self, limit: int = 10) -> list[str]:
        """Return running grid parents whose pair children are ALL terminal.

        The normal finalizer is the last pair child to finish; this query
        backstops the crash window where that child wrote its terminal status
        and died before assembling the parent result — without it the grid
        would sit at ``running`` forever with nothing left to run.

        Args:
            limit: Maximum parents to return per sweep.

        Returns:
            Parent optimization ids ready for result assembly.
        """
        session = self._get_session()
        try:
            any_child = aliased(JobModel)
            live_child = aliased(JobModel)
            has_children = (
                session.query(any_child.optimization_id)
                .filter(any_child.parent_optimization_id == JobModel.optimization_id)
                .exists()
            )
            has_live_children = (
                session.query(live_child.optimization_id)
                .filter(
                    live_child.parent_optimization_id == JobModel.optimization_id,
                    live_child.status.notin_(["success", "failed", "cancelled", "stopped"]),
                )
                .exists()
            )
            rows = (
                session.query(JobModel.optimization_id)
                .filter(JobModel.status == "running")
                .filter(has_children)
                .filter(~has_live_children)
                .limit(limit)
                .all()
            )
            return [row[0] for row in rows]
        finally:
            session.close()

    def create_grid_pair_jobs(
        self,
        parent_optimization_id: str,
        pair_count: int,
        *,
        username: str | None,
        payload_overview: dict[str, Any],
    ) -> list[str]:
        """Fan a distributed grid search out into one claimable row per pair.

        Each child row carries only a tiny reference payload — the dataset
        stays on the parent row and is re-read at claim time — plus the
        overview keys the worker pipeline dispatches on. Children inherit the
        parent's username (fairness/quota accounting) and the current code
        version, and are removed with the parent via the cascading self-FK.

        The children and the parent's flip to ``running`` commit in ONE
        transaction: a crash before the commit leaves the parent ``pending``
        (claimable through the legacy in-child grid path — the run still
        happens), while after it the parent is unclaimable and only the pair
        rows carry the work. There is no window where both paths could run
        the same grid. Calling again for a parent that already has children
        inserts nothing but still re-parks the parent — the resume path
        re-pends the children and relies on this call to flip the parent
        back to ``running``.

        Args:
            parent_optimization_id: The grid parent whose pairs are fanned out.
            pair_count: Total number of (generation, reflection) pairs.
            username: The submitting user, copied onto every child row.
            payload_overview: Minimal overview stamped on each child (at least
                the optimization-type and token-source keys the worker reads).

        Returns:
            The child optimization ids, ordered by pair index.
        """
        now = datetime.now(UTC)
        session = self._get_session()
        try:
            existing = (
                session.query(JobModel.optimization_id)
                .filter(JobModel.parent_optimization_id == parent_optimization_id)
                .order_by(JobModel.pair_index.asc())
                .all()
            )
            if existing:
                child_ids = [row[0] for row in existing]
            else:
                child_ids = [str(uuid4()) for _ in range(pair_count)]
                for index, child_id in enumerate(child_ids):
                    session.add(
                        JobModel(
                            optimization_id=child_id,
                            status="pending",
                            created_at=now,
                            latest_metrics={},
                            payload={
                                "parent_optimization_id": parent_optimization_id,
                                "pair_index": index,
                            },
                            payload_overview=dict(payload_overview),
                            optimization_type="grid_search",
                            attempts=0,
                            code_version=self._current_code_version,
                            username=username,
                            parent_optimization_id=parent_optimization_id,
                            pair_index=index,
                        )
                    )
            # The parent was claimed to reach this fan-out: clear its lease so
            # it can't be orphan-swept back to pending, and park it at
            # ``running`` — unclaimable, updated only by pair completions.
            session.query(JobModel).filter(JobModel.optimization_id == parent_optimization_id).update(
                {
                    "status": "running",
                    "message": f"Distributed across {pair_count} pair jobs",
                    "started_at": now,
                    "completed_at": None,
                    "claimed_by": None,
                    "claimed_at": None,
                    "lease_expires_at": None,
                }
            )
            session.commit()
            return child_ids
        finally:
            session.close()

    def get_grid_pair_children(self, parent_optimization_id: str) -> list[JobRecord]:
        """Return the child pair rows of a distributed grid, ordered by pair index.

        Args:
            parent_optimization_id: The grid parent whose children are read.

        Returns:
            Child ``JobRecord``s (payload omitted — it is only a tiny
            reference dict, but list callers never need it).
        """
        session = self._get_session()
        try:
            rows = (
                session.query(JobModel)
                .options(defer(JobModel.payload))
                .filter(JobModel.parent_optimization_id == parent_optimization_id)
                .order_by(JobModel.pair_index.asc())
                .all()
            )
            return [self._job_to_dict(row, include_payload=False) for row in rows]
        finally:
            session.close()

    def has_grid_pair_results(self, optimization_id: str) -> bool:
        """Return whether the grid has any completed-pair result stored.

        Args:
            optimization_id: Grid job to test.

        Returns:
            ``True`` when at least one pair result exists.
        """
        return self._grid_pair_results.has_any(optimization_id)

    def has_compatible_gepa_checkpoint(self, optimization_id: str) -> bool:
        """Expose recovery only after source, task, runtime, and integrity checks pass.

        Args:
            optimization_id: Job whose checkpoint evidence is inspected.

        Returns:
            Whether the current worker can restore every saved state blob.
        """
        try:
            job = self.get_job(optimization_id)
            if job.get("execution_budget_id") is None:
                return False
            checkpoints = self._checkpoints.list_for_optimization(optimization_id)
            if not checkpoints:
                return False
            # A run pinned to a sandbox image resumes on any worker version: its
            # optimizer, code included, lives in that image, not in the worker.
            if job.get("sandbox_image") is None and job.get("code_version") != self._current_code_version:
                return False
            for checkpoint in checkpoints:
                validate_checkpoint(
                    checkpoint.data,
                    checkpoint.manifest,
                    job.get("payload") or {},
                    job.get("code_version"),
                    sandbox_image=job.get("sandbox_image"),
                )
            return True
        except (KeyError, CheckpointCompatibilityError):
            return False

    def resumable_state_ids(self, optimization_ids: list[str]) -> set[str]:
        """Return only jobs with fully compatible persisted recovery evidence.

        Args:
            optimization_ids: Candidate job ids after cheap status checks.

        Returns:
            The subset with a supported, intact checkpoint for this runtime.
        """
        return {identifier for identifier in optimization_ids if self.has_compatible_gepa_checkpoint(identifier)}

    def requeue_for_resume(
        self,
        optimization_id: str,
        *,
        bump_attempts: bool = True,
        expected_generation: int | None = None,
        budget_service: Any = None,
        pair_index_to_resume: int | None = None,
        automatic: bool = False,
    ) -> int | None:
        """Re-queue a terminal job in place so a worker resumes it from its checkpoint.

        Flips the existing row back to ``pending`` — same id, payload, seed and
        budget — and clears the prior claim/lease. A whole-job resume increments
        ``attempts`` against the ``job_max_attempts`` cap; a targeted per-pair grid
        re-run passes ``bump_attempts=False`` so retrying individual pairs is not
        bounded by that cap. The caller owns the resumability preconditions.

        The finished leg's wall-clock duration is folded into
        ``accumulated_runtime_seconds`` before ``started_at``/``completed_at`` are
        cleared, so the resumed run's elapsed timer measures net active compute
        across all legs and excludes the paused gap between them.

        An ``automatic`` requeue recovers a run whose worker went away: it takes
        a still-active row instead of a terminal one, and a top-level run with
        no checkpoint yet starts again from the beginning. A manual resume of a
        run the orphan sweep stopped as interrupted may start again the same way.

        Args:
            optimization_id: The job to resume.
            bump_attempts: Whether to count this re-queue against the attempt cap.
            expected_generation: Fenced publication generation observed by the caller.
            budget_service: Authoritative funding and dispatch fence for a protected job.
            pair_index_to_resume: Explicit pair whose stored result is cleared atomically for recovery.
            automatic: Whether the orphan sweep recovers an interrupted active run.

        Returns:
            The new attempt count, or ``None`` when the job row is missing, was
            changed concurrently, waits for in-flight usage, or hit its budget.
        """
        session = self._get_session()
        try:
            job = (
                session.query(JobModel)
                .filter(JobModel.optimization_id == optimization_id)
                .with_for_update()
                .one_or_none()
            )
            if job is None:
                return None
            allowed = (
                {"running", "validating"} if automatic else {"failed", "cancelled", "paused", "success", "stopped"}
            )
            if job.status not in allowed or (
                expected_generation is not None and job.execution_generation != expected_generation
            ):
                return None
            checkpoints = self._checkpoints.list_for_optimization(optimization_id)
            # A run the orphan sweep stopped keeps its interrupted generation, so a
            # manual resume of it may start again like the automatic requeue.
            resumes_interrupted = (
                not automatic
                and isinstance(job.recovery, dict)
                and job.recovery.get("interrupted_budget_generation") is not None
            )
            # A grid pair or a child run needs its own checkpoint to rejoin its
            # parent's results, so only a whole top-level run may start again.
            fresh_restart = (
                (automatic or resumes_interrupted)
                and not checkpoints
                and pair_index_to_resume is None
                and job.parent_optimization_id is None
            )
            if not checkpoints and not fresh_restart:
                raise CheckpointCompatibilityError("No completed compatible checkpoint is available.")
            if pair_index_to_resume is not None and not any(
                checkpoint.pair_index == pair_index_to_resume for checkpoint in checkpoints
            ):
                raise CheckpointCompatibilityError("The selected pair has no compatible checkpoint.")
            for checkpoint in checkpoints:
                validate_checkpoint(
                    checkpoint.data,
                    checkpoint.manifest,
                    job.payload or {},
                    job.code_version,
                    sandbox_image=job.sandbox_image,
                )
            if not fresh_restart and job.sandbox_image is None and job.code_version != self._current_code_version:
                raise CheckpointCompatibilityError("No compatible worker version is available for this checkpoint.")
            if job.execution_budget_id is None:
                raise CheckpointCompatibilityError(
                    "This run has no verified cumulative execution budget; authorize a new run instead."
                )
            current_attempt = int(job.attempts or 0)
            if bump_attempts and current_attempt + 1 >= settings.job_max_attempts:
                raise CheckpointCompatibilityError("This run has reached its resume attempt limit.")
            if job.execution_budget_id is not None:
                if budget_service is None:
                    raise CheckpointCompatibilityError("Recovery requires the authoritative budget service.")
                waiting_for_usage = (
                    isinstance(job.recovery, dict)
                    and job.recovery.get("state") == "recovering"
                    and job.recovery.get("phase") == "waiting_for_usage"
                )
                if not waiting_for_usage:
                    fenced = budget_service.fence_generation(
                        job.execution_budget_id,
                        job.username,
                        expected_generation=job.execution_budget_generation,
                        session=session,
                    )
                    job.execution_budget_generation = fenced.generation
                    job.execution_generation = int(job.execution_generation or 0) + 1
                    job.payload = {**(job.payload or {}), "execution_budget_generation": fenced.generation}
                    previous = job.recovery if isinstance(job.recovery, dict) else {}
                    cause = previous.get("cause")
                    # A manual resume of a run the orphan sweep stopped carries the
                    # sweep's interrupted generation, not the one it fences now.
                    interrupted = (
                        int(fenced.generation) - 1 if automatic else previous.get("interrupted_budget_generation")
                    )
                    job.recovery = {
                        "state": "recovering",
                        "phase": "waiting_for_usage",
                        "reason": "Reconciling interrupted requests before resuming.",
                        "execution_generation": int(job.execution_generation or 0) + 1,
                        "waiting_since": datetime.now(UTC).isoformat(),
                        **({"cause": cause} if cause else {}),
                        **({"interrupted_budget_generation": interrupted} if interrupted is not None else {}),
                    }
                    fenced_generation = job.execution_generation
                    previous_status = job.status
                    session.commit()
                    job = (
                        session.query(JobModel)
                        .filter(
                            JobModel.optimization_id == optimization_id,
                            JobModel.execution_generation == fenced_generation,
                            JobModel.status == previous_status,
                        )
                        .populate_existing()
                        .with_for_update()
                        .one_or_none()
                    )
                    if job is None:
                        return None
                budget = budget_service.get(job.execution_budget_id, job.username)
                if budget.pending_operations or budget.reserved_cents:
                    tracked = _track_usage_wait(job.recovery, budget) if isinstance(job.recovery, dict) else None
                    if tracked is not None:
                        job.recovery = tracked
                        session.commit()
                    return None
                interrupted = (
                    job.recovery.get("interrupted_budget_generation") if isinstance(job.recovery, dict) else None
                )
                if automatic or interrupted is not None:
                    # The earliest pair checkpoint bounds what every pair keeps;
                    # work that settled after it runs again and is not charged twice.
                    resumed_from = (
                        None
                        if fresh_restart
                        else session.query(func.min(GepaCheckpointModel.updated_at))
                        .filter(GepaCheckpointModel.optimization_id == optimization_id)
                        .scalar()
                    )
                    budget_service.waive_interrupted_work(
                        job.execution_budget_id,
                        job.username,
                        generation=(
                            int(interrupted)
                            if interrupted is not None
                            else int(job.execution_budget_generation or 0) - 1
                        ),
                        settled_after=resumed_from,
                    )
                    budget = budget_service.get(job.execution_budget_id, job.username)
                if budget.state == "closed" and budget.available_cents > 0:
                    budget = budget_service.resume_admission(
                        job.execution_budget_id, job.username, expected_generation=job.execution_budget_generation
                    )
                if budget.available_cents <= 0 or budget.blocked_reason is not None:
                    if budget.blocked_reason is None:
                        budget_service.stop_admission(
                            job.execution_budget_id,
                            job.username,
                            reason="budget_reached",
                        )
                    _mark_resume_budget_stop(job)
                    ended_generation = int(job.execution_generation or 0)
                    session.commit()
                    if automatic:
                        self._note_ended_orphan(optimization_id, ended_generation)
                    return None
            if pair_index_to_resume is not None:
                result = job.result if isinstance(job.result, dict) else {}
                for pair in result.get("pair_results", []):
                    index = pair.get("pair_index")
                    if not isinstance(index, int) or index == pair_index_to_resume:
                        continue
                    row = session.get(GridPairResultModel, (optimization_id, index))
                    if row is None:
                        session.add(
                            GridPairResultModel(
                                optimization_id=optimization_id,
                                pair_index=index,
                                result=pair,
                                stored_bytes=json_byte_size(pair),
                            )
                        )
                selected = session.get(GridPairResultModel, (optimization_id, pair_index_to_resume))
                if selected is not None:
                    session.delete(selected)
            job.execution_generation = int(job.execution_generation or 0) + 1
            job.stop_reason = None
            job.recovery = None
            current = int(job.attempts or 0)
            next_attempt = current + 1 if bump_attempts else current
            job.attempts = next_attempt  # type: ignore[assignment]
            if fresh_restart or (automatic and job.sandbox_image is not None):
                # The sweep or the API can run on a pod of the version a deploy is
                # replacing; an unversioned row stays claimable by the workers that remain.
                job.code_version = None  # type: ignore[assignment]
            elif job.sandbox_image is not None:
                # Workers claim only their own version's jobs; a pinned image carries
                # the optimizer, so the current workers can run it.
                job.code_version = self._current_code_version  # type: ignore[assignment]
            job.status = "pending"  # type: ignore[assignment]
            job.claimed_by = None  # type: ignore[assignment]
            job.claimed_at = None  # type: ignore[assignment]
            job.lease_expires_at = None  # type: ignore[assignment]
            # Fold the just-finished leg's exact duration into the running total
            # before the timestamps are cleared, so the resumed run's elapsed timer
            # reflects net active compute across legs and never the paused gap.
            if job.started_at is not None:
                leg_start = job.started_at if job.started_at.tzinfo else job.started_at.replace(tzinfo=UTC)
                leg_end_raw = job.completed_at or datetime.now(UTC)
                leg_end = leg_end_raw if leg_end_raw.tzinfo else leg_end_raw.replace(tzinfo=UTC)
                job.accumulated_runtime_seconds = float(  # type: ignore[assignment]
                    job.accumulated_runtime_seconds or 0.0
                ) + max(0.0, (leg_end - leg_start).total_seconds())
            job.completed_at = None  # type: ignore[assignment]
            job.started_at = None  # type: ignore[assignment]
            if fresh_restart:
                job.message = "Starting again after a worker restart"  # type: ignore[assignment]
            elif automatic:
                job.message = "Resuming after a worker restart"  # type: ignore[assignment]
            else:
                job.message = "Resuming" if bump_attempts else "Re-running grid pair"  # type: ignore[assignment]
            session.commit()
            return next_attempt
        finally:
            session.close()

    def pin_sandbox_image(self, optimization_id: str, image: str) -> str:
        """Record the image a protected run executes in, keeping any earlier pin.

        Args:
            optimization_id: The run about to open its sandbox.
            image: The deployment's current image, used only when none is pinned.

        Returns:
            The image every sandbox of this run must boot from.

        Raises:
            KeyError: When the job row does not exist.
        """
        session = self._get_session()
        try:
            job = (
                session.query(JobModel)
                .filter(JobModel.optimization_id == optimization_id)
                .with_for_update()
                .one_or_none()
            )
            if job is None:
                raise KeyError(optimization_id)
            if job.sandbox_image is None:
                job.sandbox_image = image  # type: ignore[assignment]
                session.commit()
            return str(job.sandbox_image)
        finally:
            session.close()

    def requeue_for_rerun(self, optimization_id: str) -> bool:
        """Reset a terminal job in place for a clean from-scratch re-run.

        Unlike :meth:`requeue_for_resume`, which keeps the checkpoint and
        continues GEPA where it stopped, this discards every artefact of the
        previous attempt — logs, progress events, the search embedding, GEPA
        checkpoints and grid-pair results — and zeroes the row's runtime,
        result and attempt bookkeeping. Only the immutable identity (id, name,
        owner, payload) survives, so a worker re-runs the same configuration
        from iteration zero under the same id. The child-row deletes mirror
        :meth:`delete_job`, minus the ``JobModel`` row itself.

        Args:
            optimization_id: The job to reset and re-queue.

        Returns:
            ``True`` when the row existed and was reset, ``False`` when missing.
        """
        session = self._get_session()
        try:
            job = session.get(JobModel, optimization_id)
            if job is None:
                return False
            session.query(LogEntryModel).filter(LogEntryModel.optimization_id == optimization_id).delete()
            session.query(ProgressEventModel).filter(ProgressEventModel.optimization_id == optimization_id).delete()
            if settings.embeddings_enabled:
                session.query(JobEmbeddingModel).filter(JobEmbeddingModel.optimization_id == optimization_id).delete()
            session.query(GepaCheckpointModel).filter(GepaCheckpointModel.optimization_id == optimization_id).delete()
            session.query(GridPairResultModel).filter(GridPairResultModel.optimization_id == optimization_id).delete()
            session.query(BlackboxAgentRunModel).filter(
                BlackboxAgentRunModel.optimization_id == optimization_id
            ).delete()
            session.query(RunCallModel).filter(RunCallModel.optimization_id == optimization_id).delete()
            session.query(RunCandidateModel).filter(RunCandidateModel.optimization_id == optimization_id).delete()
            # A distributed grid's pair children belong to the discarded
            # attempt: drop them so the re-claimed parent fans out fresh rows
            # instead of "resuming" stale terminal children. Deleted
            # explicitly (not via the FK cascade) so SQLite test runs without
            # foreign-key enforcement behave like Postgres.
            self._delete_grid_pair_children(session, optimization_id)
            job.status = "pending"  # type: ignore[assignment]
            job.started_at = None  # type: ignore[assignment]
            job.completed_at = None  # type: ignore[assignment]
            job.estimated_remaining_seconds = None  # type: ignore[assignment]
            job.message = None  # type: ignore[assignment]
            job.latest_metrics = {}  # type: ignore[assignment]
            job.result = None  # type: ignore[assignment]
            job.attempts = 0  # type: ignore[assignment]
            job.sandbox_image = None  # type: ignore[assignment]
            job.claimed_by = None  # type: ignore[assignment]
            job.claimed_at = None  # type: ignore[assignment]
            job.lease_expires_at = None  # type: ignore[assignment]
            # Cleared so the fresh run is free to emit its own completion
            # notification — the flag is a single-shot guard set per finished run.
            job.notified_at = None  # type: ignore[assignment]
            job.notification_claims = None  # type: ignore[assignment]
            job.accumulated_runtime_seconds = 0.0  # type: ignore[assignment]
            # The result is gone, so the footprint shrinks to payload + overview.
            job.stored_bytes = sum(  # type: ignore[assignment]
                json_byte_size(getattr(job, col)) for col in _STORED_BYTES_JSON_COLUMNS
            )
            session.commit()
            return True
        finally:
            session.close()

    def get_user_quota_override(self, username: str) -> tuple[bool, int | None]:
        """Return the live quota override row for ``username`` if present.

        Args:
            username: User identifier to resolve case-insensitively.

        Returns:
            ``(False, None)`` when no row exists, otherwise ``(True, quota)``.
            A present ``None`` quota means unlimited.
        """
        normalized_username = username.strip().lower()
        if not normalized_username:
            return False, None
        session = self._get_session()
        try:
            row = session.get(UserQuotaOverrideModel, normalized_username)
            if row is None:
                return False, None
            return True, row.quota
        finally:
            session.close()

    def get_effective_user_quota(self, username: str) -> int | None:
        """Resolve a user's quota from live DB override, then static config.

        Args:
            username: User identifier to resolve.

        Returns:
            The numeric quota, or ``None`` for unlimited.
        """
        has_override, quota = self.get_user_quota_override(username)
        if has_override:
            return quota
        if self.has_pro_plan(username):
            return settings.pro_max_jobs_per_user
        return settings.get_user_quota(username)

    def has_pro_plan(self, username: str) -> bool:
        """Return whether the account's mirrored subscription grants Skynet Pro.

        Args:
            username: Account to check, resolved case-insensitively.

        Returns:
            True when the webhook-mirrored subscription currently grants Pro
            limits (a ``past_due`` one only inside its grace period).
        """
        normalized_username = username.strip().lower()
        if not normalized_username:
            return False
        session = self._get_session()
        try:
            row = session.get(BillingCustomerModel, normalized_username)
            return row is not None and has_pro_entitlement(row.subscription_status, row.subscription_past_due_since)
        finally:
            session.close()

    def get_effective_user_storage_quota(self, username: str) -> int:
        """Return the unified storage budget in bytes the user is held to.

        In local mode (the Mac app) the budget is the used bytes plus the free
        space: the smaller of the Mac disk free space and the Docker disk free
        space under the Postgres volume. Otherwise it resolves an admin
        per-user override first (a live DB row that replaces the default ceiling for that user), falling
        back to the static ``settings.user_storage_quota_bytes`` default.
        Staying a method keeps the save/run gate, the usage meter, and the
        quota modal resolving the budget through one seam that tests can stub.

        Args:
            username: Owner whose effective byte budget is resolved.

        Returns:
            The byte budget the user's total storage is checked against.
        """
        if settings.local_mode:
            # The Mac app keeps its data on the user's own disk, so the web
            # budget only blocks them (SKY-344). The budget is what Skynet
            # already uses plus the real free space. Postgres runs in a Docker
            # volume, and the Docker VM disk can fill up before the Mac disk,
            # so the smaller of the two counts.
            state_dir = Path(settings.local_state_dir)
            disk = state_dir if settings.local_state_dir and state_dir.is_dir() else Path.home()
            free = shutil.disk_usage(disk).free
            docker_free = _local_postgres_free_bytes()
            if docker_free is not None:
                free = min(free, docker_free)
            return self.compute_user_storage(username).total + free
        override = self.get_user_storage_quota_override(username)
        if override is not None:
            return override
        if self.has_pro_plan(username):
            return settings.pro_storage_quota_bytes
        return settings.user_storage_quota_bytes

    def compute_user_storage(self, username: str) -> StorageUsage:
        """Return the user's unified storage usage across every owned table.

        Args:
            username: Owner whose footprint is summed.

        Returns:
            The :class:`StorageUsage` total and per-category breakdown.
        """
        return compute_user_storage(self._engine, username)

    def compute_user_storage_items(self, username: str, limit: int = 20) -> list[StorageItem]:
        """Return the user's largest individual items for the cleanup list.

        Args:
            username: Owner whose items are ranked.
            limit: Maximum number of items to return.

        Returns:
            Up to ``limit`` :class:`StorageItem` rows ordered by descending size.
        """
        return compute_user_storage_items(self._engine, username, limit)

    def compute_user_storage_category_items(self, username: str, category: str, limit: int = 1000) -> list[StorageItem]:
        """List every deletable item the user owns in one storage category.

        Args:
            username: Owner whose items are listed.
            category: One of the deletable storage categories; any other value
                yields an empty list.
            limit: Defensive upper bound on rows returned for the category.

        Returns:
            The category's :class:`StorageItem` rows ordered by descending size.
        """
        return compute_user_storage_category_items(self._engine, username, category, limit)

    def get_user_storage_quota_override(self, username: str) -> int | None:
        """Return the per-user storage-budget override in bytes, if present.

        Args:
            username: User identifier to resolve case-insensitively.

        Returns:
            The override byte budget, or ``None`` when no override row exists.
        """
        normalized_username = username.strip().lower()
        if not normalized_username:
            return None
        session = self._get_session()
        try:
            row = session.get(UserStorageQuotaOverrideModel, normalized_username)
            return row.quota_bytes if row is not None else None
        finally:
            session.close()

    def set_user_storage_quota_override(self, username: str, quota_bytes: int, updated_by: str | None = None) -> None:
        """Create or update a per-user storage-budget override.

        Args:
            username: User identifier to store case-insensitively.
            quota_bytes: Byte ceiling that replaces the default for this user.
            updated_by: Optional operator identifier for accountability.

        Raises:
            ValueError: When ``username`` is blank or ``quota_bytes`` is below one.
        """
        normalized_username = username.strip().lower()
        if not normalized_username:
            raise ValueError("username must not be blank")
        if quota_bytes < 1:
            raise ValueError("quota_bytes must be at least 1")
        session = self._get_session()
        try:
            row = session.get(UserStorageQuotaOverrideModel, normalized_username)
            if row is None:
                row = UserStorageQuotaOverrideModel(username=normalized_username)
                session.add(row)
            row.quota_bytes = quota_bytes
            row.updated_at = datetime.now(UTC)
            row.updated_by = updated_by
            session.commit()
        finally:
            session.close()

    def delete_user_storage_quota_override(self, username: str) -> bool:
        """Delete a storage-budget override so the default budget applies again.

        Args:
            username: User identifier to clear case-insensitively.

        Returns:
            Whether a row was deleted.
        """
        normalized_username = username.strip().lower()
        if not normalized_username:
            return False
        session = self._get_session()
        try:
            deleted = (
                session.query(UserStorageQuotaOverrideModel)
                .filter(UserStorageQuotaOverrideModel.username == normalized_username)
                .delete()
            )
            session.commit()
            return bool(deleted)
        finally:
            session.close()

    def list_user_storage_quota_overrides(self) -> list[dict[str, Any]]:
        """Return all storage-budget overrides ordered by username.

        Returns:
            Override rows with ISO-formatted ``updated_at`` values.
        """
        session = self._get_session()
        try:
            rows = (
                session.query(UserStorageQuotaOverrideModel)
                .order_by(UserStorageQuotaOverrideModel.username.asc())
                .all()
            )
            return [
                {
                    "username": row.username,
                    "quota_bytes": row.quota_bytes,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    "updated_by": row.updated_by,
                }
                for row in rows
            ]
        finally:
            session.close()

    def set_user_quota_override(self, username: str, quota: int | None, updated_by: str | None = None) -> None:
        """Create or update the live quota override for a user.

        Args:
            username: User identifier to store case-insensitively.
            quota: Numeric quota, or ``None`` for unlimited.
            updated_by: Optional operator identifier for audit context.

        Raises:
            ValueError: When ``username`` is blank or ``quota`` is below one.
        """
        normalized_username = username.strip().lower()
        if not normalized_username:
            raise ValueError("username must not be blank")
        if quota is not None and quota < 1:
            raise ValueError("quota must be at least 1")
        session = self._get_session()
        try:
            row = session.get(UserQuotaOverrideModel, normalized_username)
            if row is None:
                row = UserQuotaOverrideModel(username=normalized_username)
                session.add(row)
            row.quota = quota
            row.updated_at = datetime.now(UTC)
            row.updated_by = updated_by
            session.commit()
        finally:
            session.close()

    def delete_user_quota_override(self, username: str) -> bool:
        """Delete a live quota override so config fallback applies again.

        Args:
            username: User identifier to clear case-insensitively.

        Returns:
            Whether a row was deleted.
        """
        normalized_username = username.strip().lower()
        if not normalized_username:
            return False
        session = self._get_session()
        try:
            deleted = (
                session.query(UserQuotaOverrideModel)
                .filter(UserQuotaOverrideModel.username == normalized_username)
                .delete()
            )
            session.commit()
            return bool(deleted)
        finally:
            session.close()

    def list_user_quota_overrides(self) -> list[dict[str, Any]]:
        """Return all live quota overrides ordered by username.

        Returns:
            A list of override rows with ISO-formatted ``updated_at`` values.
        """
        session = self._get_session()
        try:
            rows = session.query(UserQuotaOverrideModel).order_by(UserQuotaOverrideModel.username.asc()).all()
            return [
                {
                    "username": row.username,
                    "quota": row.quota,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    "updated_by": row.updated_by,
                }
                for row in rows
            ]
        finally:
            session.close()

    def search_usernames(self, query: str, *, limit: int = 10) -> list[str]:
        """Return distinct usernames known to the DB matching ``query``.

        Searches both job submissions (``jobs.username``) and existing quota
        overrides so admins can autocomplete previously-seen users without
        an external directory lookup.

        Args:
            query: Case-insensitive substring to match.
            limit: Maximum number of distinct usernames to return.

        Returns:
            Distinct lowercased usernames sorted alphabetically.
        """
        normalized = query.strip().lower()
        if not normalized:
            return []
        pattern = f"%{normalized}%"
        session = self._get_session()
        try:
            job_rows = (
                session.query(JobModel.username)
                .filter(JobModel.username.isnot(None))
                .filter(func.lower(JobModel.username).like(pattern))
                .distinct()
                .all()
            )
            override_rows = (
                session.query(UserQuotaOverrideModel.username)
                .filter(func.lower(UserQuotaOverrideModel.username).like(pattern))
                .all()
            )
            seen: set[str] = set()
            for (username,) in (*job_rows, *override_rows):
                if username:
                    seen.add(username.strip().lower())
            return sorted(seen)[: max(0, limit)]
        finally:
            session.close()

    def record_user_quota_audit(
        self,
        *,
        actor: str,
        target_username: str,
        action: str,
        old_quota: int | None,
        new_quota: int | None,
    ) -> None:
        """Record a quota administration audit event.

        Args:
            actor: Admin user who made the change.
            target_username: User whose quota changed.
            action: Operation name such as ``set`` or ``delete``.
            old_quota: Previous live quota, or ``None`` for unlimited/no row.
            new_quota: New live quota, or ``None`` for unlimited/default fallback.
        """
        session = self._get_session()
        try:
            session.add(
                UserQuotaAuditModel(
                    actor=actor.strip().lower(),
                    target_username=target_username.strip().lower(),
                    action=action,
                    old_quota=old_quota,
                    new_quota=new_quota,
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()
        finally:
            session.close()

    def list_user_quota_audit_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent quota administration audit events.

        Args:
            limit: Maximum number of recent events to return.

        Returns:
            Recent audit events ordered newest-first.
        """
        session = self._get_session()
        try:
            rows = (
                session.query(UserQuotaAuditModel)
                .order_by(UserQuotaAuditModel.created_at.desc(), UserQuotaAuditModel.id.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": row.id,
                    "actor": row.actor,
                    "target_username": row.target_username,
                    "action": row.action,
                    "old_quota": row.old_quota,
                    "new_quota": row.new_quota,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
        finally:
            session.close()

    def recover_orphaned_jobs(self) -> int:
        """Requeue runs whose worker went away, and fail the ones that cannot go on.

        A funded top-level run resumes from its checkpoint, or starts again when
        it has none, on any live worker. A graceful worker restart does not count
        against the attempt cap; an expired lease does, so a run that crashes
        its worker cannot loop forever. A run that waits for in-flight usage to
        settle stays active until a later sweep. When that usage makes no
        settlement progress for ``ORPHANED_USAGE_WAIT``, the run stops as
        interrupted and keeps its interrupted budget generation for a manual
        resume. A run that the sweep fails or stops waits in
        :meth:`take_ended_orphans` for its notification.

        Returns:
            Number of orphaned jobs requeued or marked failed.
        """
        session = self._get_session()
        try:
            now = datetime.now(UTC)
            child = aliased(JobModel)
            has_children = (
                session.query(child.optimization_id)
                .filter(child.parent_optimization_id == JobModel.optimization_id)
                .exists()
            )
            candidates = [
                (
                    job.optimization_id,
                    int(job.execution_generation or 0),
                    job.execution_budget_id is not None,
                    isinstance(job.recovery, dict) and job.recovery.get("cause") == WORKER_RESTART_CAUSE,
                )
                for job in session.query(JobModel)
                .filter(
                    JobModel.status.in_(["running", "validating"]),
                    (JobModel.lease_expires_at.is_(None)) | (JobModel.lease_expires_at < now),
                    ~has_children,
                )
                .all()
            ]
        finally:
            session.close()
        handled = 0
        for identifier, generation, funded, worker_restart in candidates:
            message = ORPHANED_RUN_MESSAGE
            ended_recovery: dict[str, Any] | None = None
            if funded:
                try:
                    attempt = self.requeue_for_resume(
                        identifier,
                        automatic=True,
                        bump_attempts=not worker_restart,
                        expected_generation=generation,
                        budget_service=BudgetService(engine=self._engine),
                    )
                except (CheckpointCompatibilityError, BudgetError) as exc:
                    if isinstance(exc, BudgetConflictError | BudgetInFlightError):
                        logger.info("Orphaned run %s waits for its budget to settle: %s", identifier, exc)
                        continue
                    message = f"{ORPHANED_RUN_RECOVERY_FAILED_MESSAGE} {exc}"
                except Exception:
                    logger.exception("Could not recover orphaned run %s; the next sweep retries", identifier)
                    continue
                else:
                    if attempt is not None:
                        handled += 1
                        continue
                    # The requeue attempt can restart the wait window, so the
                    # decision reads the row it left behind.
                    try:
                        waiting = self.get_job(identifier, include_payload=False)
                    except KeyError:
                        continue
                    usage_wait = waiting.get("recovery")
                    if waiting.get("execution_generation") != generation or not _usage_wait_expired(
                        usage_wait, datetime.now(UTC)
                    ):
                        logger.info(
                            "Orphaned run %s is not requeued yet; it waits for in-flight usage or changed state",
                            identifier,
                        )
                        continue
                    interrupted = usage_wait.get("interrupted_budget_generation")
                    if interrupted is None:
                        interrupted = int(waiting.get("execution_budget_generation") or 0) - 1
                    # The cause decides whether a later resume counts an attempt.
                    cause = usage_wait.get("cause")
                    ended_recovery = {
                        "state": "interrupted",
                        "interrupted_budget_generation": int(interrupted),
                        **({"cause": cause} if cause else {}),
                    }
            if self.update_job_if_status(
                identifier,
                ("running", "validating"),
                expected_generation=generation,
                status="failed",
                stop_reason="interrupted",
                completed_at=now.isoformat(),
                message=message,
                recovery=ended_recovery,
                claimed_by=None,
                claimed_at=None,
                lease_expires_at=None,
            ):
                if ended_recovery is not None:
                    logger.warning(
                        "Orphaned run %s had no usage settlement progress for %s; it stopped as interrupted",
                        identifier,
                        ORPHANED_USAGE_WAIT,
                    )
                self._note_ended_orphan(identifier, generation)
                handled += 1
        return handled

    def _note_ended_orphan(self, optimization_id: str, generation: int) -> None:
        """Remember a run that the orphan sweep ended, so its owner hears about it.

        Args:
            optimization_id: The run that the sweep failed or stopped.
            generation: Execution generation the run ended in.
        """
        with _ENDED_ORPHANS_LOCK:
            vars(self).setdefault("_ended_orphans", []).append((optimization_id, generation))

    def take_ended_orphans(self) -> list[tuple[str, int]]:
        """Return and forget the runs that orphan sweeps ended since the last call.

        The store cannot send notifications itself (the notification package
        imports the storage package), so the periodic sweeper sends them.

        Returns:
            ``(id, generation)`` of each run that a sweep failed or stopped,
            oldest first.
        """
        with _ENDED_ORPHANS_LOCK:
            return vars(self).pop("_ended_orphans", [])

    def claim_next_job(
        self,
        worker_id: str,
        lease_seconds: float,
    ) -> JobRecord | None:
        """Atomically claim the next pending job using FOR UPDATE SKIP LOCKED.

        Two pods running this method concurrently are guaranteed to see
        disjoint result sets — Postgres' ``SKIP LOCKED`` causes each session
        to silently jump over rows the other has already row-locked. The
        outer ``UPDATE`` then writes the lease metadata, completing the claim
        in one round trip.

        Ordering is fair across users, not plain FIFO: pending jobs are
        ranked by how many jobs their owner already has in flight
        (``validating``/``running``), then by age. One user submitting a
        burst can no longer monopolize every worker slot while a second
        user's single job waits behind the whole burst. The scheme is
        work-conserving — with only one user queued, their jobs still fill
        every slot in FIFO order — and starvation-free, because a job's
        rank only ever improves as its owner's running jobs finish.

        Only rows whose ``payload`` has been written are eligible. A submission
        inserts the row as ``pending`` (``create_job``) *before* the worker
        writes the payload (``submit_job``); without the ``payload IS NOT NULL``
        guard a poll tick landing in that window would claim a payload-less row
        and fail it with "has no payload". A NULL-payload row simply waits to be
        claimed until the payload lands a few milliseconds later.

        On non-PostgreSQL dialects (eg. SQLite in tests) the query falls back
        to a non-locking SELECT-then-UPDATE; that is racy but tests run
        single-threaded so it is sufficient.

        Args:
            worker_id: Identifier of the calling worker (typically pod name).
            lease_seconds: Lease duration; the worker must call
                :meth:`extend_lease` before it expires.

        Returns:
            The claimed ``JobRecord`` or ``None`` if no job was available.
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        dialect = self._engine.dialect.name
        if dialect == "postgresql":
            return self._claim_next_job_postgres(worker_id, lease_seconds)
        return self._claim_next_job_fallback(worker_id, lease_seconds)

    def _claim_next_job_postgres(self, worker_id: str, lease_seconds: float) -> JobRecord | None:
        """PostgreSQL fast path using ``FOR UPDATE SKIP LOCKED``."""
        sql = text(
            """
            UPDATE jobs
            SET status = 'validating',
                execution_generation = execution_generation + 1,
                claimed_by = :worker_id,
                claimed_at = :now,
                lease_expires_at = :lease_until
            WHERE optimization_id = (
                SELECT optimization_id FROM jobs
                WHERE status = 'pending'
                  AND payload IS NOT NULL
                  AND (code_version IS NULL OR code_version = :code_version)
                ORDER BY (
                    SELECT COUNT(*) FROM jobs r
                    WHERE r.status IN ('validating', 'running')
                      AND r.username IS NOT DISTINCT FROM jobs.username
                ) ASC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING optimization_id
            """
        )
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._engine.begin() as conn:
            result = conn.execute(
                sql,
                {
                    "worker_id": worker_id,
                    "now": now,
                    "lease_until": lease_until,
                    "code_version": self._current_code_version,
                },
            )
            row = result.fetchone()
            if row is None:
                return None
            optimization_id = row[0]

        # Re-load the row through the ORM so the returned dict matches the
        # shape of the rest of the API. The worker only reads the id off a
        # claim and fetches the payload separately once it starts the job.
        return self.get_job(optimization_id, include_payload=False)

    def _claim_next_job_fallback(self, worker_id: str, lease_seconds: float) -> JobRecord | None:
        """Best-effort claim for non-Postgres backends (tests).

        Holds an exclusive transaction so concurrent claims are serialized at
        the engine level. Not race-safe across processes but adequate for
        single-process test runs.
        """
        session = self._get_session()
        try:
            now = datetime.now(UTC)
            lease_until = now + timedelta(seconds=lease_seconds)
            candidates = (
                session.query(JobModel)
                .filter(JobModel.status == "pending")
                .filter(JobModel.payload.isnot(None))
                .filter(or_(JobModel.code_version.is_(None), JobModel.code_version == self._current_code_version))
                .order_by(JobModel.created_at.asc())
                .all()
            )
            if not candidates:
                return None
            # Same least-in-flight-per-user ordering as the Postgres path;
            # min() is stable, so ties keep FIFO order within a user.
            in_flight: dict[str | None, int] = dict(
                session.query(JobModel.username, func.count())
                .filter(JobModel.status.in_(["validating", "running"]))
                .group_by(JobModel.username)
                .all()
            )
            job = min(candidates, key=lambda row: in_flight.get(row.username, 0))
            job.status = "validating"  # type: ignore[assignment]
            job.execution_generation = int(job.execution_generation or 0) + 1
            job.claimed_by = worker_id  # type: ignore[assignment]
            job.claimed_at = now  # type: ignore[assignment]
            job.lease_expires_at = lease_until  # type: ignore[assignment]
            session.commit()
            session.refresh(job)
            return self._job_to_dict(job, include_payload=False)
        finally:
            session.close()

    def extend_lease(
        self,
        optimization_id: str,
        worker_id: str,
        lease_seconds: float,
    ) -> bool:
        """Extend the lease iff this worker still owns the claim.

        Args:
            optimization_id: ID of the job whose lease to extend.
            worker_id: Worker identity that originally claimed the job.
            lease_seconds: New lease duration measured from now.

        Returns:
            ``True`` when the lease was extended; ``False`` when the row no
            longer belongs to this worker (caller should abort processing).
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        sql = text(
            """
            UPDATE jobs
            SET lease_expires_at = :lease_until
            WHERE optimization_id = :oid
              AND claimed_by = :worker_id
            """
        )
        lease_until = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        with self._engine.begin() as conn:
            result = conn.execute(
                sql,
                {"oid": optimization_id, "worker_id": worker_id, "lease_until": lease_until},
            )
            return (result.rowcount or 0) > 0

    def release_job(self, optimization_id: str, worker_id: str) -> bool:
        """Clear claim metadata for a job, only if this worker owns it.

        Args:
            optimization_id: ID of the job to release.
            worker_id: Worker identity that claimed it.

        Returns:
            Whether claim metadata was actually cleared.
        """
        sql = text(
            """
            UPDATE jobs
            SET claimed_by = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL
            WHERE optimization_id = :oid
              AND claimed_by = :worker_id
            """
        )
        with self._engine.begin() as conn:
            result = conn.execute(sql, {"oid": optimization_id, "worker_id": worker_id})
            return (result.rowcount or 0) > 0

    def recover_pending_jobs(self) -> list[str]:
        """Return IDs of still-pending jobs ordered oldest first.

        Returns:
            Pending job IDs in FIFO order so the scheduler can
            re-enqueue them on boot.
        """
        session = self._get_session()
        try:
            rows = (
                session.query(JobModel.optimization_id)
                .filter(JobModel.status == "pending")
                .filter(or_(JobModel.code_version.is_(None), JobModel.code_version == self._current_code_version))
                .order_by(JobModel.created_at.asc())
                .all()
            )
            return [str(row[0]) for row in rows]
        finally:
            session.close()

    def set_payload_overview(self, optimization_id: str, overview: dict[str, Any]) -> None:
        """Store a summary overview of the job payload.

        The ``username`` field, if present in ``overview``, is hoisted
        to the job row so list queries don't need to parse JSON.
        Missing IDs are a silent no-op.

        Args:
            optimization_id: ID of the job to update.
            overview: Summary fields to persist.
        """
        session = self._get_session()
        try:
            job = session.query(JobModel).filter(JobModel.optimization_id == optimization_id).first()
            if job:
                job.payload_overview = overview or {}  # type: ignore[assignment]
                if "username" in (overview or {}):
                    job.username = (overview or {}).get("username")  # type: ignore[assignment]
                if "optimization_type" in (overview or {}):
                    job.optimization_type = (overview or {}).get("optimization_type")  # type: ignore[assignment]
                if "composition" in (overview or {}):
                    job.composition = (overview or {}).get("composition")  # type: ignore[assignment]
                job.stored_bytes = (  # type: ignore[assignment]
                    json_byte_size(job.payload)
                    + json_byte_size(job.result)
                    + json_byte_size(overview or {})
                    + imported_job_extra_bytes(overview, job.latest_metrics, job.terminal_evidence)
                )
                session.commit()
        finally:
            session.close()

    def record_progress_for_generation(
        self,
        optimization_id: str,
        message: str | None,
        metrics: dict[str, Any],
        *,
        source_optimization_id: str,
        generation: int,
    ) -> bool:
        """Publish progress only while its source worker owns the active generation.

        Args:
            optimization_id: Job or parent grid receiving the event.
            message: Event name.
            metrics: Completed progress evidence from the child.
            source_optimization_id: Actual leased job that emitted the event.
            generation: Publication epoch captured when the worker claimed it.

        Returns:
            Whether this still-current event was published.
        """
        session = self._get_session()
        try:
            source = (
                session.query(JobModel)
                .filter(
                    JobModel.optimization_id == source_optimization_id,
                    JobModel.execution_generation == generation,
                    JobModel.status.in_(["running", "validating"]),
                )
                .with_for_update()
                .one_or_none()
            )
            if source is None:
                return False
            target = source if source_optimization_id == optimization_id else session.get(JobModel, optimization_id)
            if target is None or target.status not in {"running", "validating"}:
                return False
            session.add(
                ProgressEventModel(
                    optimization_id=optimization_id, timestamp=datetime.now(UTC), event=message, metrics=metrics
                )
            )
            target.latest_metrics = {**(target.latest_metrics or {}), **metrics}
            session.commit()
        finally:
            session.close()
        self._finish_scored_candidate(source_optimization_id, message, metrics)
        return True

    def record_progress(self, optimization_id: str, message: str | None, metrics: dict[str, Any]) -> None:
        """Record a progress event and refresh the job's latest metrics.

        Event insertion and ``latest_metrics`` update are separate transactions
        so a transient insert failure does not roll back the UI's latest metric
        snapshot. Retention trimming runs on a sampled path to avoid counting
        rows for every high-frequency progress event.

        Args:
            optimization_id: ID of the job emitting the event.
            message: Human-readable event marker, or ``None``.
            metrics: Metric snapshot to store as ``latest_metrics``.
        """
        now = datetime.now(UTC)
        inserted = False
        try:
            inserted = self._insert_progress_event(optimization_id, message, metrics or {}, now)
        except SQLAlchemyError:
            logger.warning("Failed to insert progress event for %s", optimization_id, exc_info=True)

        if inserted and self._should_trim_progress_events(optimization_id):
            try:
                self._trim_progress_events(optimization_id)
            except SQLAlchemyError:
                logger.warning("Failed to trim progress events for %s", optimization_id, exc_info=True)

        if metrics:
            self._replace_latest_metrics(
                optimization_id,
                self._latest_metrics_with_sticky_tqdm(optimization_id, metrics),
            )
        self._finish_scored_candidate(optimization_id, message, metrics)

    def _finish_scored_candidate(self, optimization_id: str, message: str | None, metrics: dict[str, Any]) -> None:
        """Mark a scored version ``done``, or ``failed`` for a crashed case, in the run record.

        The worker writes this state: the engine that scores the version runs
        in another process, which cannot reach the run's call recorder.

        Args:
            optimization_id: Job that recorded the version's calls.
            message: Event name; ``candidate`` events and GEPA program
                ``rejected`` events change a state.
            metrics: The event's payload, with ``candidate_id`` and ``score``.
        """
        metrics = metrics or {}
        program_rejection = message == PROGRESS_REJECTED and metrics.get(PROGRAM_EVENT_KEY)
        if message != PROGRESS_CANDIDATE and not program_rejection:
            return
        key = event_candidate_key(metrics)
        if key is None:
            return
        # A version with a crashed case leaves final selection (SKY-350), so its score is None.
        state = "done" if program_rejection or metrics.get("score") is not None else "failed"
        try:
            self._run_calls.finish_candidate(optimization_id, key, state)
        except SQLAlchemyError:
            logger.warning("Failed to set a candidate state for %s", optimization_id, exc_info=True)

    def _insert_progress_event(
        self,
        optimization_id: str,
        message: str | None,
        metrics: dict[str, Any],
        timestamp: datetime,
    ) -> bool:
        """Insert one progress event if the parent job still exists.

        Args:
            optimization_id: ID of the job emitting the event.
            message: Human-readable event marker, or ``None``.
            metrics: Metric payload to persist with the event.
            timestamp: Timestamp assigned to the event row.

        Returns:
            Whether an event row was inserted.
        """
        session = self._get_session()
        try:
            exists = session.query(JobModel.optimization_id).filter(JobModel.optimization_id == optimization_id).first()
            if exists is None:
                return False
            session.add(
                ProgressEventModel(
                    optimization_id=optimization_id,
                    timestamp=timestamp,
                    event=message,
                    metrics=metrics,
                )
            )
            session.commit()
            return True
        finally:
            session.close()

    def _replace_latest_metrics(self, optimization_id: str, metrics: dict[str, Any]) -> None:
        """Replace the job's latest metrics snapshot without taking a row lock.

        Args:
            optimization_id: ID of the job to update.
            metrics: Latest metric snapshot from the subprocess.
        """
        session = self._get_session()
        try:
            session.query(JobModel).filter(JobModel.optimization_id == optimization_id).update(
                {JobModel.latest_metrics: metrics},
                synchronize_session=False,
            )
            session.commit()
        finally:
            session.close()

    def _latest_metrics_with_sticky_tqdm(self, optimization_id: str, metrics: dict[str, Any]) -> dict[str, Any]:
        """Splice the last-seen optimizer tqdm progress into a metric snapshot.

        ``latest_metrics`` is replaced wholesale on every progress event, but
        tqdm-driven optimizers (e.g. GEPA) tick their ``tqdm_*`` rollout bar only
        on ``optimizer_progress`` events while emitting many interleaved
        ``minibatch_feedback``/candidate events that carry no ``tqdm_*`` keys.
        Without carry-forward those interleaved events erase the progress bar and
        its stat cards between ticks. Remember the last-seen ``tqdm_*`` family per
        job and splice it back in so the bar persists for the life of the run.

        Args:
            optimization_id: ID of the job whose tqdm state is tracked.
            metrics: The incoming event's metric snapshot.

        Returns:
            ``metrics`` unchanged when it already carries ``tqdm_*`` keys (the
            sticky cache is refreshed in that case) or when no tqdm state has been
            seen yet; otherwise a new dict with the last-seen ``tqdm_*`` keys
            spliced beneath the incoming fields.
        """
        if not hasattr(self, "_tqdm_sticky"):
            self._tqdm_sticky = {}
            self._tqdm_sticky_lock = threading.Lock()
        incoming_tqdm = {key: value for key, value in metrics.items() if key.startswith(TQDM_KEY_PREFIX)}
        with self._tqdm_sticky_lock:
            if incoming_tqdm:
                self._tqdm_sticky[optimization_id] = incoming_tqdm
                return metrics
            carried = self._tqdm_sticky.get(optimization_id)
        if not carried:
            return metrics
        return {**carried, **metrics}

    def _should_trim_progress_events(self, optimization_id: str) -> bool:
        """Return whether this event should trigger a sampled retention trim.

        Args:
            optimization_id: ID of the job whose in-memory event counter is advanced.

        Returns:
            ``True`` every ``PROGRESS_TRIM_SAMPLE_RATE`` events for a job.
        """
        if not hasattr(self, "_progress_event_counters"):
            self._progress_event_counters = defaultdict(int)
            self._progress_counter_lock = threading.Lock()
        with self._progress_counter_lock:
            self._progress_event_counters[optimization_id] += 1
            return self._progress_event_counters[optimization_id] % PROGRESS_TRIM_SAMPLE_RATE == 0

    def _trim_progress_events(self, optimization_id: str) -> None:
        """Delete the oldest excess progress events for a job in one batch.

        Args:
            optimization_id: ID of the job whose retained progress rows should
                be brought back down to the configured cap.
        """
        session = self._get_session()
        try:
            event_count = (
                session.query(ProgressEventModel).filter(ProgressEventModel.optimization_id == optimization_id).count()
            )
            excess = event_count - self._progress_events_cap
            if excess <= 0 or self._is_auto_run(session, optimization_id):
                return

            # One ordered DELETE replaces the prior count→select→top-up→delete
            # round trips. Structural events sort last (flag 1) so they are
            # evicted only when non-structural rows alone cannot cover the
            # excess — identical preference to the old two-phase selection.
            structural_last = case(
                (ProgressEventModel.event.in_(STRUCTURAL_PROGRESS_EVENTS), 1),
                else_=0,
            )
            old_ids = (
                session.query(ProgressEventModel.id)
                .filter(ProgressEventModel.optimization_id == optimization_id)
                .order_by(structural_last.asc(), ProgressEventModel.timestamp.asc(), ProgressEventModel.id.asc())
                .limit(excess)
            )
            session.query(ProgressEventModel).filter(ProgressEventModel.id.in_(old_ids.scalar_subquery())).delete(
                synchronize_session=False
            )
            session.commit()
        finally:
            session.close()

    @staticmethod
    def _is_auto_run(session: Session, optimization_id: str) -> bool:
        """Return whether a job ran Auto lanes, whose full timing is kept for engine analysis.

        The lane-started markers decide it rather than the payload because legacy
        and unified submissions store the strategy in different shapes, and the
        markers are structural, so they outlive every other row a trim could drop.

        Args:
            session: Open database session.
            optimization_id: ID of the job being trimmed.

        Returns:
            ``True`` once the job has recorded an explore or continue lane.
        """
        lane_metrics = (
            session.query(ProgressEventModel.metrics)
            .filter(
                ProgressEventModel.optimization_id == optimization_id,
                ProgressEventModel.event == PROGRESS_LANE_STARTED,
            )
            .all()
        )
        return any(
            isinstance(metrics, dict) and metrics.get("phase") in AUTO_LANE_PHASES for (metrics,) in lane_metrics
        )

    def get_progress_events(self, optimization_id: str, *, since: int = 0) -> list[ProgressEventRecord]:
        """Retrieve progress events for a job in chronological order.

        Args:
            optimization_id: ID of the job to inspect.
            since: Number of leading events to skip, for tail (delta) fetches;
                ``0`` returns the full history.

        Returns:
            Events ordered oldest-first, starting at offset ``since``.
        """
        session = self._get_session()
        try:
            query = (
                session.query(ProgressEventModel)
                .filter(ProgressEventModel.optimization_id == optimization_id)
                .order_by(ProgressEventModel.timestamp.asc(), ProgressEventModel.id.asc())
            )
            if since > 0:
                query = query.offset(since)
            events = query.all()
            return [
                cast(
                    ProgressEventRecord,
                    {
                        "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                        "event": e.event,
                        "metrics": e.metrics or {},
                    },
                )
                for e in events
            ]
        finally:
            session.close()

    def get_progress_count(self, optimization_id: str) -> int:
        """Return the number of progress events recorded for a job.

        Args:
            optimization_id: ID of the job to inspect.

        Returns:
            Number of stored events.
        """
        session = self._get_session()
        try:
            return (
                session.query(ProgressEventModel).filter(ProgressEventModel.optimization_id == optimization_id).count()
            )
        finally:
            session.close()

    def append_log(
        self,
        optimization_id: str,
        *,
        level: str,
        logger_name: str,
        message: str,
        timestamp: datetime | None = None,
        pair_index: int | None = None,
        source: str | None = None,
        event: str | None = None,
        fields: dict[str, Any] | None = None,
        candidate: str | None = None,
        case: str | None = None,
    ) -> None:
        """Append a log entry for a job.

        Silently discards the entry if the job no longer exists (a late log
        from a cleaned-up run is not an error). A run's log is never trimmed:
        its rows count against the owner's storage budget instead.

        Args:
            optimization_id: ID of the job emitting the log.
            level: Log level string (``INFO``, ``ERROR``, ...).
            logger_name: Originating logger name.
            message: Log line content.
            timestamp: Optional override for the entry timestamp; defaults to ``now``.
            pair_index: Optional grid-pair index when emitted from a sweep.
            source: Where the line came from, see :data:`core.run_log.SOURCES`.
            event: Typed event name, if the line is an event.
            fields: The event's JSON payload.
            candidate: Candidate the line belongs to.
            case: Case the line belongs to.
        """
        self.append_logs(
            optimization_id,
            [
                {
                    "level": level,
                    "logger": logger_name,
                    "message": message,
                    "timestamp": timestamp,
                    "pair_index": pair_index,
                    "source": source,
                    "event": event,
                    "fields": fields,
                    "candidate": candidate,
                    "case": case,
                }
            ],
        )

    def append_logs(self, optimization_id: str, entries: list[dict[str, Any]]) -> None:
        """Append several log entries for a job in one transaction.

        Takes the keys :meth:`append_log` takes as keyword arguments, with
        ``logger`` in place of ``logger_name``. Text bound for a fixed-width
        column is cut to fit and ends with ``...``. Once the owner's storage
        is full the run stores one notice and drops every later entry, see
        :mod:`core.storage.log_quota`.

        Args:
            optimization_id: ID of the job emitting the logs.
            entries: The entries, oldest first.
        """
        if not entries:
            return
        now = datetime.now(UTC)
        session = self._get_session()
        try:
            # An existence probe, not a critical section: appends are independent
            # inserts, so a per-job row lock would only serialize writers and
            # starve the connection pool under concurrent log bursts.
            owner = session.query(JobModel.username).filter(JobModel.optimization_id == optimization_id).first()
            if owner is None:
                logger.warning("Discarding %d log entries for missing job %s", len(entries), optimization_id)
                return
            if owner.username:
                # Fail open: a usage lookup that errors must not lose the run's
                # log, and logging the failure here would re-enter this method.
                with contextlib.suppress(Exception):
                    entries = self._log_quota.admit(optimization_id, owner.username, entries)
            if not entries:
                return
            session.add_all(
                LogEntryModel(
                    optimization_id=optimization_id,
                    timestamp=entry.get("timestamp") or now,
                    level=run_log.fit(str(entry.get("level") or "INFO"), run_log.LEVEL_CHARS),
                    logger=run_log.fit(str(entry.get("logger") or ""), run_log.LOGGER_CHARS),
                    # Postgres TEXT rejects NUL, which sandbox output can carry.
                    message=str(entry.get("message") or "").replace("\x00", ""),
                    pair_index=entry.get("pair_index"),
                    source=run_log.fit(entry.get("source"), run_log.SOURCE_CHARS),
                    event=run_log.fit(entry.get("event"), run_log.EVENT_CHARS),
                    fields=entry.get("fields"),
                    candidate=run_log.fit(entry.get("candidate"), run_log.CANDIDATE_CHARS),
                    case_id=run_log.fit(entry.get("case"), run_log.CASE_CHARS),
                )
                for entry in entries
            )
            session.commit()
        finally:
            session.close()

    def get_logs(
        self,
        optimization_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        level: str | None = None,
        after_id: int | None = None,
    ) -> list[LogEntryRecord]:
        """Retrieve log entries for a job, ordered ascending.

        Args:
            optimization_id: ID of the job to inspect.
            limit: Maximum number of entries to return; ``None`` means no cap.
            offset: Number of entries to skip.
            level: When set, restricts results to the given level.
            after_id: When set, returns only entries written after the entry with this id,
                in write order, which is what a live reader resumes from.

        Returns:
            Matching log entries in chronological order.
        """
        session = self._get_session()
        try:
            q = session.query(LogEntryModel).filter(LogEntryModel.optimization_id == optimization_id)
            if level:
                q = q.filter(LogEntryModel.level == level)
            if after_id is not None:
                q = q.filter(LogEntryModel.id > after_id).order_by(LogEntryModel.id.asc())
            else:
                q = q.order_by(LogEntryModel.timestamp.asc(), LogEntryModel.id.asc())
            if offset:
                q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)
            return [_log_record(log) for log in q.all()]
        finally:
            session.close()

    def get_log_count(self, optimization_id: str, *, level: str | None = None) -> int:
        """Return the number of log entries for a job, optionally filtered by level.

        Args:
            optimization_id: ID of the job to inspect.
            level: When set, counts only entries at this level.

        Returns:
            Number of matching log entries.
        """
        session = self._get_session()
        try:
            q = session.query(LogEntryModel).filter(LogEntryModel.optimization_id == optimization_id)
            if level:
                q = q.filter(LogEntryModel.level == level)
            return q.count()
        finally:
            session.close()

    def scan_jobs_for_analytics(
        self,
        *,
        status: str | None = None,
        username: str | None = None,
        limit: int = 10000,
    ) -> list[JobRecord]:
        """Skinny newest-first job scan for the analytics KPI rollups.

        Rows are shaped like :meth:`list_jobs` output but carry only the
        fields the aggregations read: ``status`` plus pruned
        ``payload_overview`` and ``result`` dicts. The pruning happens in the
        SELECT itself — the full ``result`` blob (per-example outputs,
        multi-MB for grid runs) never leaves the database, which is what
        keeps a 10k-row scan from spiking the API process. JSON paths are
        extracted with SQLAlchemy's dialect-portable operators, so the same
        query runs on PostgreSQL and the SQLite test harness.

        Args:
            status: Restrict to jobs with this status when set.
            username: Restrict to jobs owned by this user when set.
            limit: Maximum number of rows to scan, newest first.

        Returns:
            Skinny job rows in newest-first order.
        """
        overview = JobModel.payload_overview
        session = self._get_session()
        try:
            q = session.query(
                JobModel.optimization_id,
                JobModel.status,
                overview[PAYLOAD_OVERVIEW_OPTIMIZER_NAME].as_string(),
                overview[PAYLOAD_OVERVIEW_MODEL_NAME].as_string(),
                overview[PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE].as_string(),
                overview[PAYLOAD_OVERVIEW_DATASET_ROWS].as_string(),
                overview[PAYLOAD_OVERVIEW_TOTAL_PAIRS].as_string(),
                *_result_summary_columns(),
            ).order_by(JobModel.created_at.desc())
            q = q.filter(_user_facing_jobs())
            if status:
                q = q.filter(JobModel.status == status)
            if username:
                q = q.filter(JobModel.username == username)
            rows = q.limit(limit).all()
        finally:
            session.close()
        return [_assemble_analytics_row(row) for row in rows]

    def _list_query(self, session: Session):
        """Base SELECT shared by the list surfaces.

        Every ``jobs`` column except the two JSON blobs, plus the ``result``
        scalars from :func:`_result_summary_columns`. ``payload`` (the
        training set) and ``result`` (per-example outputs, program artifacts)
        are the multi-MB members of a row and no list reader looks past the
        summary scalars, so neither leaves the database.

        Args:
            session: The open session to build the query on.

        Returns:
            A query yielding ``(JobModel, *result scalars)`` rows.
        """
        return session.query(JobModel, *_result_summary_columns()).options(
            defer(JobModel.payload), defer(JobModel.result)
        )

    def _list_row_to_dict(self, row: Any) -> JobRecord:
        """Convert one :meth:`_list_query` row to a ``JobRecord`` carrying the pruned ``result``.

        Args:
            row: A ``(JobModel, *result scalars)`` row.

        Returns:
            The ``JobRecord`` with ``payload`` omitted and ``result`` summarized.
        """
        job, *summary = row
        record = self._job_to_dict(job, include_payload=False, include_result=False)
        record["result"] = _assemble_result_summary(summary)
        return record

    def list_jobs(
        self,
        *,
        status: str | None = None,
        username: str | None = None,
        optimization_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        with_counts: bool = True,
    ) -> list[JobRecord]:
        """List jobs with optional filtering and pagination, newest first.

        Progress and log counts are folded in via two aggregate
        queries so each returned row includes ``progress_count`` and
        ``log_count`` without N extra round trips. Internal tagger bulk-job
        rows are excluded unless ``optimization_type`` requests them.

        Args:
            status: Restrict to jobs with this status when set.
            username: Restrict to jobs owned by this user when set.
            optimization_type: Restrict to a particular run type when set.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip from the start.
            with_counts: When ``False``, skip the progress/log/summary
                aggregate folding entirely — analytics rollups scan up to 10k
                rows and never read those fields, so the aggregates would run
                a 10k-element ``IN (...)`` scan for nothing.

        Returns:
            Matching ``JobRecord`` rows in newest-first order, with
            ``progress_count`` / ``log_count`` populated when ``with_counts``.
            ``result`` carries only its summary scalars (see
            :func:`_assemble_result_summary`); :meth:`get_job` returns the
            full blob.
        """
        session = self._get_session()
        try:
            q = self._list_query(session).order_by(JobModel.created_at.desc())
            if status:
                q = q.filter(JobModel.status == status)
            if username:
                q = q.filter(JobModel.username == username)
            if optimization_type:
                q = q.filter(JobModel.optimization_type == optimization_type, _top_level_jobs())
            else:
                q = q.filter(_user_facing_jobs())
            records = [self._list_row_to_dict(row) for row in q.offset(offset).limit(limit).all()]
            if not with_counts:
                return records
            return self._rows_with_counts(session, records)
        finally:
            session.close()

    def list_jobs_by_ids(self, optimization_ids: list[str]) -> list[JobRecord]:
        """List the user-facing jobs among ``optimization_ids``, newest first.

        Backs folder contents, where membership (not ownership) picks the rows;
        the caller has already resolved access to each id.

        Args:
            optimization_ids: Ids to load. Unknown ids are skipped.

        Returns:
            Matching ``JobRecord`` rows shaped like :meth:`list_jobs` rows,
            without the progress/log count folding.
        """
        if not optimization_ids:
            return []
        session = self._get_session()
        try:
            q = (
                self._list_query(session)
                .filter(JobModel.optimization_id.in_(optimization_ids), _user_facing_jobs())
                .order_by(JobModel.created_at.desc())
            )
            return [self._list_row_to_dict(row) for row in q.all()]
        finally:
            session.close()

    def _rows_with_counts(self, session: Session, records: list[JobRecord]) -> list[JobRecord]:
        """Fold progress/log counts and summary text into list rows.

        Two aggregate queries (plus the embedding lookup) keyed on the page's
        ``optimization_ids`` so each row carries ``progress_count`` /
        ``log_count`` / ``summary_text`` without an N-per-row round trip. Shared
        by :meth:`list_jobs`, :meth:`list_jobs_shared_with` and
        :meth:`list_jobs_visible_to`.

        Args:
            session: The open session the ``records`` were loaded on.
            records: The page of :meth:`_list_row_to_dict` rows to enrich in place.

        Returns:
            The same rows with the folded counts.
        """
        optimization_ids = [str(record["optimization_id"]) for record in records]
        progress_counts: dict[str, int] = (
            {
                row[0]: row[1]
                for row in session.query(ProgressEventModel.optimization_id, func.count())
                .filter(ProgressEventModel.optimization_id.in_(optimization_ids))
                .group_by(ProgressEventModel.optimization_id)
                .all()
            }
            if optimization_ids
            else {}
        )
        log_counts: dict[str, int] = (
            {
                row[0]: row[1]
                for row in session.query(LogEntryModel.optimization_id, func.count())
                .filter(LogEntryModel.optimization_id.in_(optimization_ids))
                .group_by(LogEntryModel.optimization_id)
                .all()
            }
            if optimization_ids
            else {}
        )
        # summary_text lives in job_embeddings, which only exists on the
        # semantic backend; skip the lookup entirely on the lexical backend so
        # the join target isn't a missing relation (callers fall back to None).
        summary_texts: dict[str, str | None] = (
            {
                row[0]: row[1]
                for row in session.query(
                    JobEmbeddingModel.optimization_id,
                    JobEmbeddingModel.summary_text,
                )
                .filter(JobEmbeddingModel.optimization_id.in_(optimization_ids))
                .all()
            }
            if optimization_ids and settings.embeddings_enabled
            else {}
        )
        for record in records:
            oid = str(record["optimization_id"])
            record["progress_count"] = progress_counts.get(oid, 0)
            record["log_count"] = log_counts.get(oid, 0)
            record["summary_text"] = summary_texts.get(oid)
        return records

    def list_jobs_shared_with(self, username: str, *, limit: int = 50, offset: int = 0) -> list[JobRecord]:
        """List jobs shared with ``username`` via a member grant, newest first.

        Joins ``optimization_share_grants`` on ``grantee_username`` so a member
        sees runs they were invited to but do not own. Grants the member hid
        from their own view are skipped. The same count-folding as
        :meth:`list_jobs` is applied; the grant role is not attached here (the
        caller resolves roles for the page via
        :func:`core.api.sharing_access.list_grants_for_user`).

        Args:
            username: Grantee username (compared case-insensitively).
            limit: Maximum number of rows to return.
            offset: Number of rows to skip from the start.

        Returns:
            Matching ``JobRecord`` rows in newest-first order, with ``result``
            pruned to its summary scalars as in :meth:`list_jobs`.
        """
        normalized = username.strip().lower()
        session = self._get_session()
        try:
            rows = (
                self._list_query(session)
                .join(
                    OptimizationShareGrantModel,
                    OptimizationShareGrantModel.optimization_id == JobModel.optimization_id,
                )
                .filter(OptimizationShareGrantModel.grantee_username == normalized)
                .filter(OptimizationShareGrantModel.hidden_at.is_(None))
                .filter(_user_facing_jobs())
                .order_by(JobModel.created_at.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            return self._rows_with_counts(session, [self._list_row_to_dict(row) for row in rows])
        finally:
            session.close()

    def count_jobs_shared_with(self, username: str) -> int:
        """Count jobs shared with ``username`` via a member grant.

        Args:
            username: Grantee username (compared case-insensitively).

        Returns:
            Number of optimizations the user holds a grant on.
        """
        normalized = username.strip().lower()
        session = self._get_session()
        try:
            return (
                session.query(func.count(OptimizationShareGrantModel.optimization_id))
                .join(
                    JobModel,
                    OptimizationShareGrantModel.optimization_id == JobModel.optimization_id,
                )
                .filter(OptimizationShareGrantModel.grantee_username == normalized)
                .filter(OptimizationShareGrantModel.hidden_at.is_(None))
                .filter(_user_facing_jobs())
                .scalar()
                or 0
            )
        finally:
            session.close()

    def list_jobs_visible_to(
        self,
        username: str,
        *,
        status: str | None = None,
        optimization_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        with_counts: bool = True,
    ) -> list[JobRecord]:
        """List jobs the caller owns or was granted access to, newest first.

        Unions the caller's own jobs with jobs shared to them via a member
        grant (Drive-style sharing), de-duplicated, ordered newest-first and
        paginated as a single set. Powers the unified control panel, where a
        collaborator sees their own runs and runs shared with them together.

        Args:
            username: The caller. Owned rows match exactly (mirroring
                :meth:`list_jobs`); grant rows match case-insensitively
                because grants store a lowercased grantee.
            status: Restrict to jobs with this status when set.
            optimization_type: Restrict to a particular run type when set.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip from the start.
            with_counts: When ``False``, skip the progress/log/summary
                aggregate folding, as in :meth:`list_jobs` — the dashboard
                analytics scan never reads those fields.

        Returns:
            Matching ``JobRecord`` rows in newest-first order with
            ``progress_count`` / ``log_count`` / ``summary_text`` folded in
            when ``with_counts``, and ``result`` pruned to its summary
            scalars as in :meth:`list_jobs`.
        """
        normalized = username.strip().lower()
        session = self._get_session()
        try:
            grant_ids = session.query(OptimizationShareGrantModel.optimization_id).filter(
                OptimizationShareGrantModel.grantee_username == normalized,
                OptimizationShareGrantModel.hidden_at.is_(None),
            )
            q = self._list_query(session).filter(
                or_(JobModel.username == username, JobModel.optimization_id.in_(grant_ids))
            )
            if status:
                q = q.filter(JobModel.status == status)
            if optimization_type:
                q = q.filter(JobModel.optimization_type == optimization_type, _top_level_jobs())
            else:
                q = q.filter(_user_facing_jobs())
            rows = q.order_by(JobModel.created_at.desc()).offset(offset).limit(limit).all()
            records = [self._list_row_to_dict(row) for row in rows]
            if not with_counts:
                return records
            return self._rows_with_counts(session, records)
        finally:
            session.close()

    def count_jobs_visible_to(
        self, username: str, *, status: str | None = None, optimization_type: str | None = None
    ) -> int:
        """Count jobs the caller owns or was granted access to.

        The union counterpart of :meth:`list_jobs_visible_to`; matching rules
        are identical.

        Args:
            username: The caller (owned exact, grants case-insensitive).
            status: Restrict count to this status when set.
            optimization_type: Restrict count to this run type when set.

        Returns:
            Number of optimizations visible to the caller under the filters.
        """
        normalized = username.strip().lower()
        session = self._get_session()
        try:
            grant_ids = session.query(OptimizationShareGrantModel.optimization_id).filter(
                OptimizationShareGrantModel.grantee_username == normalized,
                OptimizationShareGrantModel.hidden_at.is_(None),
            )
            q = session.query(func.count(JobModel.optimization_id)).filter(
                or_(JobModel.username == username, JobModel.optimization_id.in_(grant_ids))
            )
            if status:
                q = q.filter(JobModel.status == status)
            if optimization_type:
                q = q.filter(JobModel.optimization_type == optimization_type, _top_level_jobs())
            else:
                q = q.filter(_user_facing_jobs())
            return q.scalar() or 0
        finally:
            session.close()

    def count_users(self) -> int:
        """Count total registered accounts.

        Backs the sign-up cap (:data:`core.config.settings.max_total_users`): the
        registration path refuses new accounts once this reaches the cap. The
        count is advisory — it runs outside the insert's transaction, so a small
        overshoot at the boundary under concurrent sign-ups is acceptable.

        Returns:
            The number of rows in the ``users`` table.
        """
        session = self._get_session()
        try:
            return session.query(func.count(UserModel.email)).scalar() or 0
        finally:
            session.close()

    def count_monthly_active_users(self, month_start: date) -> int:
        """Count identities admitted during one UTC calendar month.

        Args:
            month_start: First UTC date of the month to count.

        Returns:
            Number of distinct admitted identities for the month.
        """
        session = self._get_session()
        try:
            return (
                session.query(func.count(MonthlyActiveUserModel.username))
                .filter(MonthlyActiveUserModel.month_start == month_start)
                .scalar()
                or 0
            )
        finally:
            session.close()

    def admit_monthly_active_user(self, username: str, month_start: date, limit: int) -> bool:
        """Atomically admit one identity if monthly capacity remains.

        A transaction-scoped advisory lock serializes only first-seen monthly
        identities across API replicas. Returning users hit the composite key
        fast path, while PgBouncer transaction mode safely releases the lock at
        commit.

        Args:
            username: Normalized authenticated identity.
            month_start: First UTC date of the month being admitted.
            limit: Maximum distinct identities allowed for the month.

        Returns:
            ``True`` for an existing or newly admitted identity; ``False`` when
            the month is already at capacity.
        """
        session = self._get_session()
        try:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 742137000004})
            key = (month_start, username)
            if session.get(MonthlyActiveUserModel, key) is not None:
                session.commit()
                return True
            count = (
                session.query(func.count(MonthlyActiveUserModel.username))
                .filter(MonthlyActiveUserModel.month_start == month_start)
                .scalar()
                or 0
            )
            if count >= limit:
                session.rollback()
                return False
            session.add(MonthlyActiveUserModel(month_start=month_start, username=username))
            session.commit()
            return True
        finally:
            session.close()

    def count_jobs_by_status(self, *, username: str | None = None) -> dict[str, int]:
        """Count jobs per status in a single ``GROUP BY`` query.

        One round trip replaces the per-status ``count_jobs`` fan-out on the
        dashboard stat-card path; bucket values are identical.

        Args:
            username: Restrict counts to this owner when set.

        Returns:
            Mapping of status name to row count (statuses with no rows are
            simply absent).
        """
        session = self._get_session()
        try:
            q = session.query(JobModel.status, func.count(JobModel.optimization_id)).filter(_user_facing_jobs())
            if username:
                q = q.filter(JobModel.username == username)
            return dict(q.group_by(JobModel.status).all())
        finally:
            session.close()

    def count_jobs_by_status_visible_to(self, username: str) -> dict[str, int]:
        """Count jobs per status among rows the caller owns or holds a grant on.

        The single-round-trip union counterpart of :meth:`count_jobs_by_status`;
        matching rules mirror :meth:`count_jobs_visible_to`.

        Args:
            username: The caller (owned exact, grants case-insensitive).

        Returns:
            Mapping of status name to visible-row count.
        """
        normalized = username.strip().lower()
        session = self._get_session()
        try:
            grant_ids = session.query(OptimizationShareGrantModel.optimization_id).filter(
                OptimizationShareGrantModel.grantee_username == normalized,
                OptimizationShareGrantModel.hidden_at.is_(None),
            )
            q = session.query(JobModel.status, func.count(JobModel.optimization_id)).filter(
                or_(JobModel.username == username, JobModel.optimization_id.in_(grant_ids)),
                _user_facing_jobs(),
            )
            return dict(q.group_by(JobModel.status).all())
        finally:
            session.close()

    def count_jobs(
        self, *, status: str | None = None, username: str | None = None, optimization_type: str | None = None
    ) -> int:
        """Count jobs matching the given filters.

        Args:
            status: Restrict count to this status when set.
            username: Restrict count to this owner when set.
            optimization_type: Restrict count to this run type when set.

        Returns:
            Number of matching rows.
        """
        session = self._get_session()
        try:
            q = session.query(func.count(JobModel.optimization_id))
            if status:
                q = q.filter(JobModel.status == status)
            if username:
                q = q.filter(JobModel.username == username)
            if optimization_type:
                q = q.filter(JobModel.optimization_type == optimization_type, _top_level_jobs())
            else:
                q = q.filter(_user_facing_jobs())
            return q.scalar() or 0
        finally:
            session.close()

    def get_queue_metrics(self) -> tuple[int, float]:
        """Return pending-job depth and oldest pending-job age in seconds.

        Returns:
            ``(pending_count, queue_age_seconds)`` with age set to ``0.0`` when
            no jobs are pending.
        """
        if self._engine.dialect.name == "postgresql":
            with self._engine.connect() as conn:
                row = conn.execute(
                    text(
                        """
                        SELECT
                            count(*)::int AS pending_count,
                            COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at))), 0) AS queue_age_seconds
                        FROM jobs
                        WHERE status = 'pending'
                        """
                    )
                ).one()
            return int(row[0] or 0), float(row[1] or 0.0)

        session = self._get_session()
        try:
            pending_count, oldest_created_at = (
                session.query(func.count(JobModel.optimization_id), func.min(JobModel.created_at))
                .filter(JobModel.status == "pending")
                .one()
            )
            if oldest_created_at is None:
                return int(pending_count or 0), 0.0
            if oldest_created_at.tzinfo is None:
                oldest_created_at = oldest_created_at.replace(tzinfo=UTC)
            age_seconds = max(0.0, (datetime.now(UTC) - oldest_created_at).total_seconds())
            return int(pending_count or 0), age_seconds
        finally:
            session.close()

    def stage_dataset(self, username: str, dataset_filename: str, rows: list[dict[str, Any]]) -> str:
        """Persist wizard-parsed dataset rows for an agent-driven submit.

        Args:
            username: Submitter who owns the staged copy.
            dataset_filename: Original filename for diagnostics.
            rows: Parsed dataset rows; must be non-empty.

        Returns:
            The opaque staged-dataset id used by ``/run``.

        Raises:
            ValueError: When ``rows`` is empty.
        """
        if not rows:
            raise ValueError("staged dataset rows must be non-empty")
        staged_id = uuid4().hex
        session = self._get_session()
        try:
            session.add(
                AgentStagedDatasetModel(
                    id=staged_id,
                    username=username,
                    dataset_filename=dataset_filename,
                    rows=rows,
                    row_count=len(rows),
                )
            )
            session.commit()
            return staged_id
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    def get_staged_dataset(self, staged_dataset_id: str, username: str) -> list[dict[str, Any]] | None:
        """Fetch staged rows by id, scoped to the calling user.

        Args:
            staged_dataset_id: Id previously returned by ``stage_dataset``.
            username: Authenticated caller — scope guards against cross-user reads.

        Returns:
            The persisted rows, or ``None`` when the row is missing or owned
            by another user.
        """
        session = self._get_session()
        try:
            row = (
                session.query(AgentStagedDatasetModel)
                .filter(
                    AgentStagedDatasetModel.id == staged_dataset_id,
                    AgentStagedDatasetModel.username == username,
                )
                .one_or_none()
            )
            if row is None:
                return None
            return list(row.rows)
        finally:
            session.close()

    def delete_staged_dataset(self, staged_dataset_id: str, username: str) -> bool:
        """Drop a staged dataset once it has been consumed.

        Args:
            staged_dataset_id: Id to evict.
            username: Authenticated caller — scope guards against cross-user deletes.

        Returns:
            ``True`` when a row was deleted, ``False`` when none matched.
        """
        session = self._get_session()
        try:
            deleted = (
                session.query(AgentStagedDatasetModel)
                .filter(
                    AgentStagedDatasetModel.id == staged_dataset_id,
                    AgentStagedDatasetModel.username == username,
                )
                .delete(synchronize_session=False)
            )
            session.commit()
            return bool(deleted)
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

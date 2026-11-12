"""Tests for the explore search embedding pipeline.

These tests stub out the embedder, summariser, and Session so the pure
Python logic (metadata extraction, upsert branches, backfill queueing)
is covered without a live pgvector instance.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, inspect, text

from core.service_gateway.embedding_pipeline import core as pipeline


class _FakeEmbedder:
    """Minimal embedder double used by the tests.

    ``encode`` returns a small deterministic vector so call-count and
    argument shape can be asserted without a live embedding API.
    """

    def __init__(self, *, available: bool = True, vector: list[float] | None = None) -> None:
        """Configure availability and the canned vector returned by ``encode``."""
        self._available = available
        self._vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self.encode_calls: list[tuple[str, str | None]] = []

    def available(self) -> bool:
        """Return whether this embedder is configured as available."""
        return self._available

    def encode(self, text: str, *, task: str | None = None, user: str | None = None) -> list[float] | None:
        """Record the call and return the canned vector (or ``None`` for blanks)."""
        self.encode_calls.append((text, task))
        if not text or not text.strip():
            return None
        return list(self._vector)


class _FakeJobStore:
    """Carries just the attributes the pipeline touches."""

    def __init__(self, jobs: dict[str, dict[str, Any]] | None = None) -> None:
        """Seed the in-memory job map and an opaque engine handle."""
        self._jobs = jobs or {}
        self.engine = MagicMock(name="engine")

    def get_job(self, optimization_id: str) -> dict[str, Any]:
        """Return the seeded job dict, raising ``KeyError`` if unknown."""
        if optimization_id not in self._jobs:
            raise KeyError(optimization_id)
        return self._jobs[optimization_id]


def _success_job(**overrides: Any) -> dict[str, Any]:
    """Build a minimal successful run-job dict with optional overrides."""
    job: dict[str, Any] = {
        "status": "success",
        "payload_overview": {
            "username": "alice",
            "model_name": "openai/gpt-4o-mini",
            "optimization_type": "run",
            "name": "Sentiment task",
        },
        "payload": {
            "signature_code": "class S(dspy.Signature):\n    q = dspy.InputField()\n    a = dspy.OutputField()",
            "metric_code": "def metric(gold, pred):\n    return gold.a == pred.a",
            "column_mapping": {"inputs": {"q": "question"}, "outputs": {"a": "answer"}},
            "dataset": [{"question": "hi", "answer": "hello"}],
            "module_name": "dspy.Predict",
            "optimizer_name": "MIPROv2",
        },
        "latest_metrics": {"baseline_test_metric": 50.0, "optimized_test_metric": 72.5},
        "result": None,
    }
    job.update(overrides)
    return job


def test_extract_metadata_run_returns_overview_model() -> None:
    """A run job's winner is the configured model name on the overview."""
    job = _success_job()
    model, job_type = pipeline._extract_metadata(job)
    assert model == "openai/gpt-4o-mini"
    assert job_type == "run"


def test_extract_scores_run_uses_latest_metrics() -> None:
    """A run job reads ``latest_metrics.baseline/optimized``."""
    job = _success_job()
    baseline, optimized = pipeline._extract_scores(job)
    assert baseline == 50.0
    assert optimized == 72.5


def test_extract_scores_run_falls_back_to_result_baseline() -> None:
    """A run job recovers the baseline from ``result`` when latest_metrics drops it.

    GEPA run jobs only keep ``optimized_test_metric`` in latest_metrics at
    completion; the baseline survives in ``result``. Without the fallback the
    embedded baseline is NULL and the gain sort collapses to raw score.
    """
    job = _success_job(
        latest_metrics={"optimized_test_metric": 90.0},
        result={"baseline_test_metric": 85.0, "optimized_test_metric": 90.0},
    )
    baseline, optimized = pipeline._extract_scores(job)
    assert baseline == 85.0
    assert optimized == 90.0


def test_extract_scores_handles_non_numeric_metrics() -> None:
    """Non-numeric metric values coerce to ``None`` instead of raising."""
    job = _success_job(latest_metrics={"baseline_test_metric": "n/a", "optimized_test_metric": None})
    baseline, optimized = pipeline._extract_scores(job)
    assert baseline is None
    assert optimized is None


def test_extract_display_fields_prefers_overview_name() -> None:
    """``task_name`` falls back to ``payload.name`` only when overview lacks it."""
    job = _success_job()
    fields = pipeline._extract_display_fields(job)
    assert fields == {
        "task_name": "Sentiment task",
        "module_name": "dspy.Predict",
        "optimizer_name": "MIPROv2",
    }


def test_extract_display_fields_falls_back_to_payload_name() -> None:
    """A missing overview name falls back to ``payload.name``."""
    job = _success_job(
        payload_overview={"username": "alice", "optimization_type": "run"},
        payload={"name": "Fallback name", "module_name": "M", "optimizer_name": "O"},
    )
    fields = pipeline._extract_display_fields(job)
    assert fields["task_name"] == "Fallback name"


def test_embed_finished_job_skips_when_disabled() -> None:
    """``embeddings_enabled=False`` short-circuits the pipeline."""
    store = _FakeJobStore({"job-1": _success_job()})
    with patch.object(pipeline.settings, "embeddings_enabled", False):
        assert pipeline.embed_finished_job("job-1", job_store=store) is False


def test_embed_finished_job_skips_when_embedder_unavailable() -> None:
    """An unavailable embedder short-circuits before touching the DB."""
    store = _FakeJobStore({"job-1": _success_job()})
    embedder = _FakeEmbedder(available=False)
    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=embedder),
    ):
        assert pipeline.embed_finished_job("job-1", job_store=store) is False
    assert embedder.encode_calls == []


def test_embed_finished_job_skips_missing_job() -> None:
    """A ``KeyError`` from ``get_job`` is swallowed; nothing is written."""
    store = _FakeJobStore({})
    embedder = _FakeEmbedder()
    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=embedder),
    ):
        assert pipeline.embed_finished_job("missing", job_store=store) is False


def test_embed_finished_job_skips_non_success_status() -> None:
    """A job that did not succeed is ignored — backfill won't surface it either."""
    store = _FakeJobStore({"job-1": _success_job(status="failed")})
    embedder = _FakeEmbedder()
    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=embedder),
    ):
        assert pipeline.embed_finished_job("job-1", job_store=store) is False


def test_embed_finished_job_skips_when_summary_empty() -> None:
    """An empty summary (no usable text) skips the upsert entirely."""
    store = _FakeJobStore({"job-1": _success_job()})
    embedder = _FakeEmbedder()
    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=embedder),
        patch.object(pipeline, "summarize_task", return_value=""),
    ):
        assert pipeline.embed_finished_job("job-1", job_store=store) is False
    assert embedder.encode_calls == []


def test_embed_finished_job_writes_summary_row(monkeypatch) -> None:
    """A happy-path call inserts a new row with summary embedding + display fields."""
    store = _FakeJobStore({"job-1": _success_job()})
    embedder = _FakeEmbedder(vector=[0.5, 0.5, 0.5])

    session = MagicMock(name="session")
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.query.return_value.filter.return_value.first.return_value = None

    added: list[Any] = []
    session.add.side_effect = added.append

    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=embedder),
        patch.object(pipeline, "summarize_task", return_value="A short task summary."),
        patch.object(pipeline, "Session", return_value=session),
    ):
        assert pipeline.embed_finished_job("job-1", job_store=store) is True

    assert len(added) == 1
    row = added[0]
    assert row.optimization_id == "job-1"
    assert row.embedding_summary == [0.5, 0.5, 0.5]
    assert row.task_name == "Sentiment task"
    assert row.module_name == "dspy.Predict"
    assert row.optimizer_name == "MIPROv2"
    assert row.winning_model == "openai/gpt-4o-mini"
    assert row.optimization_type == "run"
    assert row.summary_text == "A short task summary."
    session.commit.assert_called_once()


def test_embed_finished_job_updates_existing_row() -> None:
    """An existing row is mutated in-place rather than re-inserted."""
    store = _FakeJobStore({"job-1": _success_job()})
    embedder = _FakeEmbedder(vector=[0.9, 0.0, 0.0])

    existing = MagicMock(name="existing_row")
    session = MagicMock(name="session")
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.query.return_value.filter.return_value.first.return_value = existing

    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=embedder),
        patch.object(pipeline, "summarize_task", return_value="Refreshed summary."),
        patch.object(pipeline, "Session", return_value=session),
    ):
        assert pipeline.embed_finished_job("job-1", job_store=store) is True

    session.add.assert_not_called()
    assert existing.embedding_summary == [0.9, 0.0, 0.0]
    assert existing.summary_text == "Refreshed summary."
    assert existing.updated_at.tzinfo is not None
    session.commit.assert_called_once()


def test_embed_finished_job_swallows_session_failure() -> None:
    """A DB error during commit returns ``False`` instead of raising."""
    store = _FakeJobStore({"job-1": _success_job()})
    embedder = _FakeEmbedder()

    session = MagicMock(name="session")
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.query.return_value.filter.return_value.first.return_value = None
    session.commit.side_effect = RuntimeError("pgvector connection lost")

    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=embedder),
        patch.object(pipeline, "summarize_task", return_value="A task summary."),
        patch.object(pipeline, "Session", return_value=session),
    ):
        assert pipeline.embed_finished_job("job-1", job_store=store) is False


def test_fetch_missing_embedding_ids_returns_ids() -> None:
    """The scan unwraps ``optimization_id`` values from the LEFT JOIN."""
    store = _FakeJobStore()
    session = MagicMock(name="session")
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.execute.return_value.mappings.return_value.all.return_value = [
        {"optimization_id": "a"},
        {"optimization_id": "b"},
    ]
    with patch.object(pipeline, "Session", return_value=session):
        ids = pipeline._fetch_missing_embedding_ids(store)
    assert ids == ["a", "b"]


def test_fetch_missing_embedding_ids_swallows_db_failure() -> None:
    """A SQL-side failure degrades to an empty list."""
    store = _FakeJobStore()
    session = MagicMock(name="session")
    session.__enter__ = MagicMock(side_effect=RuntimeError("DB down"))
    with patch.object(pipeline, "Session", return_value=session):
        assert pipeline._fetch_missing_embedding_ids(store) == []


def test_backfill_returns_zero_when_disabled() -> None:
    """``embeddings_enabled=False`` skips even the scan."""
    store = _FakeJobStore()
    with patch.object(pipeline.settings, "embeddings_enabled", False):
        assert pipeline.backfill_missing_embeddings(store) == 0


def test_backfill_returns_zero_when_nothing_missing() -> None:
    """When the scan returns no IDs, no thread is started and 0 is returned."""
    store = _FakeJobStore()
    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "_fetch_missing_embedding_ids", return_value=[]),
        patch.object(pipeline.threading, "Thread") as thread_cls,
    ):
        assert pipeline.backfill_missing_embeddings(store) == 0
        thread_cls.assert_not_called()


def test_backfill_queues_drain_thread() -> None:
    """When IDs exist, a daemon thread is started and the count is returned."""
    store = _FakeJobStore()
    fake_thread = MagicMock(name="thread")
    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "_fetch_missing_embedding_ids", return_value=["a", "b", "c"]),
        patch.object(pipeline.threading, "Thread", return_value=fake_thread) as thread_cls,
    ):
        assert pipeline.backfill_missing_embeddings(store) == 3
        thread_cls.assert_called_once()
        kwargs = thread_cls.call_args.kwargs
        assert kwargs["daemon"] is True
        assert kwargs["name"] == "embed-backfill"
        fake_thread.start.assert_called_once()


def test_drain_backfill_queue_processes_each_id() -> None:
    """The drain calls ``embed_finished_job`` for every queued id in order."""
    store = _FakeJobStore()
    calls: list[str] = []

    def _fake_embed(optimization_id: str, *, job_store: Any) -> bool:
        """Stub embedder that records each call and reports success."""
        calls.append(optimization_id)
        return True

    with patch.object(pipeline, "embed_finished_job", side_effect=_fake_embed):
        pipeline._drain_backfill_queue(store, ["a", "b", "c"])
    assert calls == ["a", "b", "c"]


def test_drain_backfill_queue_continues_after_embed_failure() -> None:
    """A raised embed_finished_job does not abort the drain."""
    store = _FakeJobStore()
    seen: list[str] = []

    def _fake_embed(optimization_id: str, *, job_store: Any) -> bool:
        """Stub embedder that raises on the middle id."""
        seen.append(optimization_id)
        if optimization_id == "b":
            raise RuntimeError("flaky LLM")
        return True

    with patch.object(pipeline, "embed_finished_job", side_effect=_fake_embed):
        pipeline._drain_backfill_queue(store, ["a", "b", "c"])
    assert seen == ["a", "b", "c"]


def test_fetch_all_success_ids_returns_ids() -> None:
    """The corpus-wide scan unwraps every success job's ``optimization_id``."""
    store = _FakeJobStore()
    session = MagicMock(name="session")
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.execute.return_value.mappings.return_value.all.return_value = [
        {"optimization_id": "a"},
        {"optimization_id": "b"},
    ]
    with patch.object(pipeline, "Session", return_value=session):
        ids = pipeline._fetch_all_success_ids(store)
    assert ids == ["a", "b"]


def test_reembed_all_returns_zero_when_disabled() -> None:
    """``embeddings_enabled=False`` skips even the scan."""
    store = _FakeJobStore()
    with (
        patch.object(pipeline.settings, "embeddings_enabled", False),
        patch.object(pipeline, "_fetch_all_success_ids", side_effect=AssertionError("scanned")),
    ):
        assert pipeline.reembed_all_embeddings(store) == 0


def test_reembed_all_drains_every_success_id() -> None:
    """Every success id is re-embedded synchronously and the written count returned."""
    store = _FakeJobStore()
    calls: list[str] = []

    def _fake_embed(optimization_id: str, *, job_store: Any) -> bool:
        """Stub embedder that records each call and reports success."""
        calls.append(optimization_id)
        return True

    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "_fetch_all_success_ids", return_value=["a", "b", "c"]),
        patch.object(pipeline, "embed_finished_job", side_effect=_fake_embed),
    ):
        assert pipeline.reembed_all_embeddings(store) == 3
    assert calls == ["a", "b", "c"]


def test_embedding_index_sweeper_repairs_a_bounded_batch() -> None:
    """A repair pass purges orphans and drains only the configured batch."""
    store = _FakeJobStore()
    store.engine = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    sweeper = pipeline.EmbeddingIndexSweeper(store, interval_seconds=5.0, batch_size=2)
    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "purge_orphan_embeddings", return_value=0) as purge,
        patch.object(pipeline, "_fetch_missing_embedding_ids", return_value=["a", "b"]) as fetch,
        patch.object(pipeline, "_drain_backfill_queue", return_value=2) as drain,
    ):
        assert sweeper.sweep_once() == 2
    purge.assert_called_once_with(store)
    fetch.assert_called_once_with(store, limit=2)
    drain.assert_called_once_with(store, ["a", "b"])


def test_set_embedding_task_name_updates_row() -> None:
    """A rename propagates the new display name onto the embedding row."""
    store = _FakeJobStore()
    session = MagicMock(name="session")
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)

    with patch.object(pipeline, "Session", return_value=session):
        pipeline.set_embedding_task_name(store, "job-1", "Renamed task")

    update = session.query.return_value.filter.return_value.update
    update.assert_called_once()
    values = update.call_args.args[0]
    assert values[pipeline.JobEmbeddingModel.task_name] == "Renamed task"
    assert values[pipeline.JobEmbeddingModel.updated_at].tzinfo is not None
    session.commit.assert_called_once()


def test_set_embedding_task_name_noop_without_engine() -> None:
    """A store with no SQLAlchemy engine (offline mode) is left untouched."""
    store = _FakeJobStore()
    store.engine = None

    with patch.object(pipeline, "Session") as session_factory:
        pipeline.set_embedding_task_name(store, "job-1", "Renamed task")

    session_factory.assert_not_called()


def _blackbox_success_job(**overrides: Any) -> dict[str, Any]:
    """Build a minimal successful black-box job dict with optional overrides."""
    job: dict[str, Any] = {
        "status": "success",
        "payload_overview": {
            "username": "alice",
            "model_name": "google/gemini-3-flash-preview",
            "optimization_type": "blackbox",
            "name": "3D unicorn",
            "description": "Build a nicer unicorn",
            "module_name": "blackbox",
            "optimizer_name": "auto",
        },
        "payload": {
            "objective": "Make the rendered unicorn look more like a unicorn.",
            "background": "build123d script rendered with pyrender and judged by a VLM.",
            "recipe": "code",
            "seed_candidate": "def build():\n    return Sphere(1)",
            "scorer": {"kind": "python", "metric_code": "def score(v, c):\n    return 0.5"},
            "cases": [{"prompt": "a unicorn"}],
        },
        "latest_metrics": {},
        "result": {
            "engine_used": "gepa",
            "baseline_test_metric": 0.2,
            "optimized_test_metric": 0.5075,
        },
    }
    job.update(overrides)
    return job


def test_extract_display_fields_blackbox_falls_back_to_overview_and_engine_used() -> None:
    """A black-box payload has no module/optimizer; the overview and result fill them in."""
    fields = pipeline._extract_display_fields(_blackbox_success_job())
    assert fields == {"task_name": "3D unicorn", "module_name": "blackbox", "optimizer_name": "gepa"}


def test_extract_display_fields_blackbox_keeps_overview_engine_without_result() -> None:
    """Without a persisted result the overview optimizer name (the strategy engine) stands."""
    fields = pipeline._extract_display_fields(_blackbox_success_job(result=None))
    assert fields["optimizer_name"] == "auto"


def test_embed_finished_job_blackbox_uses_blackbox_summariser() -> None:
    """Black-box jobs are summarised from title/description/cases, not the DSPy path."""
    store = _FakeJobStore({"bb-1": _blackbox_success_job()})
    embedder = _FakeEmbedder(vector=[0.1, 0.2, 0.3])

    session = MagicMock(name="session")
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.query.return_value.filter.return_value.first.return_value = None
    added: list[Any] = []
    session.add.side_effect = added.append

    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=embedder),
        patch.object(pipeline, "summarize_task", side_effect=AssertionError("DSPy path used")),
        patch.object(pipeline, "summarize_blackbox_task", return_value="Improves a unicorn model.") as bb,
        patch.object(pipeline, "Session", return_value=session),
    ):
        assert pipeline.embed_finished_job("bb-1", job_store=store) is True

    kwargs = bb.call_args.kwargs
    assert kwargs["title"] == "3D unicorn"
    assert kwargs["description"] == "Build a nicer unicorn"
    assert kwargs["cases_sample"] == [{"prompt": "a unicorn"}]
    row = added[0]
    assert row.optimization_type == "blackbox"
    assert row.module_name == "blackbox"
    assert row.optimizer_name == "gepa"
    assert row.winning_model == "google/gemini-3-flash-preview"
    assert row.baseline_metric == 0.2
    assert row.optimized_metric == 0.5075
    assert row.summary_text == "Improves a unicorn model."
    assert row.signature_code is None


def _summary_session() -> MagicMock:
    """Return a Session double that finds no existing embedding row."""
    session = MagicMock(name="session")
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.query.return_value.filter.return_value.first.return_value = None
    return session


def test_embed_finished_job_meters_the_summary_to_the_owner() -> None:
    """An owner with a funded balance gets the LLM summary and is billed for it as managed usage."""
    store = _FakeJobStore({"job-1": _success_job()})
    lm = object()

    def _summarize(**kwargs: Any) -> str:
        """Stand in for the summariser: record the LM like the real one does."""
        assert kwargs["use_llm"] is True
        kwargs["usage_sink"].append(lm)
        return "A summary."

    billing = MagicMock()
    billing.return_value.spendable_cents.return_value = 12
    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=_FakeEmbedder()),
        patch.object(pipeline, "StripeBillingService", billing),
        patch.object(pipeline, "summarize_task", side_effect=_summarize),
        patch.object(pipeline, "meter_llm_run") as meter,
        patch.object(pipeline, "Session", return_value=_summary_session()),
    ):
        assert pipeline.embed_finished_job("job-1", job_store=store) is True

    billing.return_value.spendable_cents.assert_called_once_with("alice")
    meter.assert_called_once_with(store.engine, "alice", [lm], description="Job summary", token_source="managed")


def test_embed_finished_job_uses_free_summary_when_owner_has_no_balance() -> None:
    """A broke owner still gets indexed, via the heuristic summary, and is not billed."""
    store = _FakeJobStore({"job-1": _success_job()})
    billing = MagicMock()
    billing.return_value.spendable_cents.return_value = 0
    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=_FakeEmbedder()),
        patch.object(pipeline, "StripeBillingService", billing),
        patch.object(pipeline, "summarize_task", return_value="Sentiment task") as summarize,
        patch.object(pipeline, "meter_llm_run") as meter,
        patch.object(pipeline, "Session", return_value=_summary_session()),
    ):
        assert pipeline.embed_finished_job("job-1", job_store=store) is True

    assert summarize.call_args.kwargs["use_llm"] is False
    meter.assert_not_called()


def test_embed_finished_job_never_bills_a_desktop_import() -> None:
    """A run imported from the desktop gets the free summary even when its owner is funded."""
    job = _success_job()
    job["payload"] = None
    job["payload_overview"]["imported_from"] = "desktop"
    store = _FakeJobStore({"job-1": job})
    billing = MagicMock()
    billing.return_value.spendable_cents.return_value = 5000
    with (
        patch.object(pipeline.settings, "embeddings_enabled", True),
        patch.object(pipeline, "get_embedder", return_value=_FakeEmbedder()),
        patch.object(pipeline, "StripeBillingService", billing),
        patch.object(pipeline, "summarize_task", return_value="Sentiment task") as summarize,
        patch.object(pipeline, "meter_llm_run") as meter,
        patch.object(pipeline, "Session", return_value=_summary_session()),
    ):
        assert pipeline.embed_finished_job("job-1", job_store=store) is True

    assert summarize.call_args.kwargs["use_llm"] is False
    billing.return_value.spendable_cents.assert_not_called()
    meter.assert_not_called()


def _sqlite_store(*, with_embedding_table: bool) -> SimpleNamespace:
    """Build a job store on in-memory SQLite, with or without a slim ``job_embeddings`` table.

    The slim table holds only the columns the privacy and name sync touch, so the
    real ORM update runs without pgvector.

    Args:
        with_embedding_table: Create the table and seed row ``opt-1`` (public, named ``old``).

    Returns:
        An object exposing ``engine`` like the real job store.
    """
    engine = create_engine("sqlite://")
    if with_embedding_table:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE job_embeddings (optimization_id VARCHAR(36) PRIMARY KEY, "
                    "is_private BOOLEAN NOT NULL, task_name VARCHAR, updated_at TIMESTAMP)"
                )
            )
            conn.execute(text("INSERT INTO job_embeddings VALUES ('opt-1', 0, 'old', NULL)"))
    return SimpleNamespace(engine=engine)


def _embedding_row(store: SimpleNamespace) -> tuple[Any, ...]:
    """Return ``(is_private, task_name)`` of the seeded ``opt-1`` embedding row."""
    with store.engine.connect() as conn:
        return tuple(
            conn.execute(text("SELECT is_private, task_name FROM job_embeddings WHERE optimization_id = 'opt-1'")).one()
        )


def test_set_embedding_privacy_skips_without_embedding_table() -> None:
    """The Mac app database has no ``job_embeddings`` table, so the sync is a no-op, not an error."""
    store = _sqlite_store(with_embedding_table=False)
    with (
        patch.object(pipeline.settings, "embeddings_enabled", False),
        patch.object(pipeline, "Session") as session_cls,
    ):
        pipeline.set_embedding_privacy(store, "opt-1", True)
    session_cls.assert_not_called()
    assert not inspect(store.engine).has_table("job_embeddings")


def test_set_embedding_privacy_updates_kept_table_with_embeddings_off() -> None:
    """A row kept from an earlier semantic run is still updated, so a Private job never shows in Explore."""
    store = _sqlite_store(with_embedding_table=True)
    with patch.object(pipeline.settings, "embeddings_enabled", False):
        pipeline.set_embedding_privacy(store, "opt-1", True)
    assert _embedding_row(store) == (1, "old")


def test_set_embedding_task_name_skips_without_embedding_table() -> None:
    """A rename in the Mac app must not fail on the missing ``job_embeddings`` table."""
    store = _sqlite_store(with_embedding_table=False)
    with patch.object(pipeline, "Session") as session_cls:
        pipeline.set_embedding_task_name(store, "opt-1", "new")
    session_cls.assert_not_called()
    assert not inspect(store.engine).has_table("job_embeddings")


def test_set_embedding_task_name_updates_row_with_embedding_table() -> None:
    """The rename is copied onto an existing embedding row."""
    store = _sqlite_store(with_embedding_table=True)
    pipeline.set_embedding_task_name(store, "opt-1", "new")
    assert _embedding_row(store) == (0, "new")

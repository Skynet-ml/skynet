"""Tests for Prometheus observability helpers."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from core.billing.operation_pricing import json_fingerprint

from .. import observability as obs


class _QueueMetricStore:
    """Fake store exposing cached queue metric inputs."""

    def get_queue_metrics(self) -> tuple[int, float]:
        """Return a stable pending count and queue age."""
        return 3, 42.5


def test_metrics_endpoint_exposes_cached_queue_gauges() -> None:
    """The Prometheus scrape includes queue-depth and queue-age gauges."""
    if obs._Instrumentator is None or obs._Gauge is None:
        pytest.skip("Prometheus metrics dependencies are not installed")
    app = FastAPI()
    obs.install_metrics(app)
    obs.QueueMetricsRefresher(_QueueMetricStore()).refresh_once()

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert "skynet_jobs_pending 3.0" in response.text
    assert "skynet_queue_age_seconds 42.5" in response.text


class _OrphanStore:
    """Fake store recording orphan-recovery calls."""

    def __init__(self, recovered: int = 2, engine: Any | None = None) -> None:
        """Capture how many orphans the fake will report and the fake engine."""
        self.calls = 0
        self._recovered = recovered
        self.engine = engine

    def recover_orphaned_jobs(self) -> int:
        """Count invocations and return the configured recovery count."""
        self.calls += 1
        return self._recovered


def test_orphan_sweeper_runs_on_non_postgres_dialect() -> None:
    """SQLite / no-engine stores skip the lock check and run the sweep directly."""
    store = _OrphanStore(recovered=4, engine=None)
    sweeper = obs.OrphanRecoverySweeper(store, interval_seconds=5.0)

    assert sweeper.sweep_once() == 4
    assert store.calls == 1


def test_model_reconciliation_uses_original_digest_and_bounded_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep usage recovery on its original credential and advance only a bounded page.

    Args:
        monkeypatch: Test-owned dependency and settings replacements.
    """
    observed: list[tuple[int, str | None]] = []

    def sweep(*, limit: int, after_id: str | None):
        """Record one non-network reconciliation page.

        Args:
            limit: Maximum attempts allowed this tick.
            after_id: Previous tick's continuation identity.

        Returns:
            A page requiring one subsequent continuation.
        """
        observed.append((limit, after_id))
        return SimpleNamespace(results=[], next_cursor="next")

    def reconciler(service: Any, resolver: Any):
        """Verify credential resolution before returning the isolated sweep.

        Args:
            service: Unused fake ledger.
            resolver: Trusted digest-to-key function.

        Returns:
            Fake bounded reconciliation implementation.
        """
        assert resolver("alice", json_fingerprint("original-managed-key")) == "original-managed-key"
        assert resolver("alice", json_fingerprint("rotated-key")) is None
        return SimpleNamespace(sweep=sweep)

    monkeypatch.setattr(obs, "BudgetService", lambda **kwargs: object())
    monkeypatch.setattr(obs, "OpenRouterUsageReconciler", reconciler)
    monkeypatch.setattr(obs.settings, "openrouter_api_key", SecretStr("original-managed-key"))
    monkeypatch.setattr(obs.settings, "vercel_token", None)
    sweeper = obs.OrphanRecoverySweeper(_OrphanStore())
    sweeper._reconcile_sandbox_usage(object())
    sweeper._reconcile_sandbox_usage(object())
    assert observed == [(4, None), (4, "next")]


def _paged_reconciler(pages: list[list[tuple[str, str, str | None]]]) -> Any:
    """Build a fake reconciler class that returns one scripted page per sweep.

    Args:
        pages: For each tick, the (operation id, state, reason) results.

    Returns:
        A callable that accepts the reconciler arguments and returns the fake.
    """
    remaining = list(pages)

    def sweep(*, limit: int, after_id: str | None) -> SimpleNamespace:
        """Return the next scripted page.

        Args:
            limit: Unused page size.
            after_id: Unused cursor.

        Returns:
            The next page with no continuation cursor.
        """
        results = [SimpleNamespace(operation_id=o, state=s, reason=r) for o, s, r in remaining.pop(0)]
        return SimpleNamespace(results=results, next_cursor=None)

    return lambda *args: SimpleNamespace(sweep=sweep)


def _pending_lines(caplog: pytest.LogCaptureFixture, kind: str) -> list[str]:
    """Return the captured INFO lines that report a pending operation of one kind.

    Args:
        caplog: Captured log records.
        kind: Line prefix, for example "Vercel" or "Model".

    Returns:
        Formatted messages in emission order.
    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and record.getMessage().startswith(f"{kind} usage for operation ")
    ]


def test_vercel_pending_reason_logs_once_per_operation_and_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Show why a Vercel hold stays pending without repeating the same line every tick.

    A settle forgets the operation, so a later pending tick with the same reason logs again.

    Args:
        monkeypatch: Test-owned dependency and settings replacements.
        caplog: Captured log records.
    """
    monkeypatch.setattr(obs, "BudgetService", lambda **kwargs: object())
    monkeypatch.setattr(obs, "OpenRouterUsageReconciler", _paged_reconciler([[]] * 5))
    monkeypatch.setattr(obs, "VercelSessionUsageClient", lambda *args: object())
    monkeypatch.setattr(
        obs,
        "VercelUsageReconciler",
        _paged_reconciler(
            [
                [("op-1", "pending", "first reason"), ("op-2", "settled", None)],
                [("op-1", "pending", "first reason")],
                [("op-1", "pending", "second reason")],
                [("op-1", "settled", None)],
                [("op-1", "pending", "second reason")],
            ]
        ),
    )
    monkeypatch.setattr(obs.settings, "vercel_token", SecretStr("token"))
    monkeypatch.setattr(obs.settings, "vercel_team_id", "team")
    caplog.set_level(logging.INFO, logger=obs.logger.name)
    sweeper = obs.OrphanRecoverySweeper(_OrphanStore())

    for _ in range(5):
        sweeper._reconcile_sandbox_usage(object())

    assert _pending_lines(caplog, "Vercel") == [
        "Vercel usage for operation op-1 stays pending: first reason",
        "Vercel usage for operation op-1 stays pending: second reason",
        "Vercel usage for operation op-1 stays pending: second reason",
    ]
    assert "op-2" not in sweeper._logged_pending_reasons


def test_model_pending_reason_logs_once_per_operation_and_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Show why a model hold stays pending without repeating the same line every tick.

    Args:
        monkeypatch: Test-owned dependency and settings replacements.
        caplog: Captured log records.
    """
    monkeypatch.setattr(obs, "BudgetService", lambda **kwargs: object())
    monkeypatch.setattr(
        obs,
        "OpenRouterUsageReconciler",
        _paged_reconciler(
            [
                [("op-1", "pending", "no generation yet"), ("op-2", "settled", None)],
                [("op-1", "pending", "no generation yet")],
                [("op-1", "pending", "key rotated")],
            ]
        ),
    )
    monkeypatch.setattr(obs.settings, "vercel_token", None)
    caplog.set_level(logging.INFO, logger=obs.logger.name)
    sweeper = obs.OrphanRecoverySweeper(_OrphanStore())

    for _ in range(3):
        sweeper._reconcile_sandbox_usage(object())

    assert _pending_lines(caplog, "Model") == [
        "Model usage for operation op-1 stays pending: no generation yet",
        "Model usage for operation op-1 stays pending: key rotated",
    ]


def test_pending_reason_memory_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the once-per-pair memory small when many holds never settle.

    Args:
        monkeypatch: Test-owned dependency and settings replacements.
    """
    many = [(f"op-{index}", "pending", "reason") for index in range(obs.PENDING_REASON_MEMORY_LIMIT + 50)]
    monkeypatch.setattr(obs, "BudgetService", lambda **kwargs: object())
    monkeypatch.setattr(obs, "OpenRouterUsageReconciler", _paged_reconciler([many]))
    monkeypatch.setattr(obs.settings, "vercel_token", None)
    sweeper = obs.OrphanRecoverySweeper(_OrphanStore())

    sweeper._reconcile_sandbox_usage(object())

    assert len(sweeper._logged_pending_reasons) == obs.PENDING_REASON_MEMORY_LIMIT


def test_orphan_sweeper_swallows_recovery_exceptions() -> None:
    """A raising ``recover_orphaned_jobs`` must not propagate out of the loop."""

    class _Raises:
        engine = None

        def recover_orphaned_jobs(self) -> int:
            """Raise to verify the sweeper swallows recovery failures."""
            raise RuntimeError("boom")

    sweeper = obs.OrphanRecoverySweeper(_Raises(), interval_seconds=5.0)
    assert sweeper.sweep_once() == 0


def test_advisory_lock_yields_true_for_non_postgres_dialect() -> None:
    """SQLite / no-engine callers treat the lock as won so single-process tests run the work."""
    with obs.advisory_lock(None, 12345) as won:
        assert won is True


def test_orphan_sweeper_skips_when_advisory_lock_held() -> None:
    """On Postgres, a peer holding the advisory lock yields a no-op sweep."""

    class _PGDialect:
        name = "postgresql"

    class _Conn:
        def execute(self, _stmt: Any, _params: Any) -> Any:
            """Return a result whose ``scalar()`` reports the lock as not won."""

            class _Result:
                def scalar(self) -> bool:
                    """Report the advisory lock as held by a peer."""
                    return False

            return _Result()

    class _Begin:
        def __enter__(self) -> _Conn:
            """Enter a fake SQLAlchemy transaction context."""
            return _Conn()

        def __exit__(self, *_exc: Any) -> None:
            """Exit the fake transaction without committing."""
            return

    class _Engine:
        dialect = _PGDialect()

        def begin(self) -> _Begin:
            """Return a fake transaction context for advisory-lock checks."""
            return _Begin()

    store = _OrphanStore(recovered=7, engine=_Engine())
    sweeper = obs.OrphanRecoverySweeper(store, interval_seconds=5.0)

    assert sweeper.sweep_once() == 0
    assert store.calls == 0

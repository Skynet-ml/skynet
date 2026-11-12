"""Verify the worker gates black-box jobs on protected execution before any dispatch."""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from core.constants import OPTIMIZATION_TYPE_BLACKBOX
from core.storage.base import JobStore
from core.worker import engine

from .conftest import FakeJobStore
from .mocks import REAL_RUN_PAYLOAD, FakeGateway, make_mp_context, make_result_event, protect_dispatch


def _payload() -> dict[str, Any]:
    """Build a protected black-box payload with its budget binding.

    Returns:
        A protected black-box payload accepted by the request schema.
    """
    return {
        **REAL_RUN_PAYLOAD,
        "username": "alice",
        "execution_budget_id": "budget",
        "execution_budget_revision": 1,
        "execution_budget_generation": 0,
        "max_cost_cents": 20,
    }


def test_budget_gateway_closes_while_the_lease_renews(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the claim alive while the gateway waits for in-flight usage, so the sweep cannot race it.

    Args:
        monkeypatch: Parent credential fixture.
    """
    store = FakeJobStore()
    store.engine = object()
    store.seed_job(
        "lease-close",
        payload=_payload(),
        payload_overview={
            "optimization_type": OPTIMIZATION_TYPE_BLACKBOX,
            "username": "alice",
            "token_source": "managed",
        },
        execution_budget_id="budget",
        execution_budget_generation=0,
    )
    worker = engine.BackgroundWorker(job_store=cast(JobStore, store), num_workers=1)
    worker.enqueue_job("lease-close")
    context, _process = make_mp_context(result_events=[make_result_event()])
    monkeypatch.setattr(engine.settings, "openrouter_api_key", SecretStr("fixture-only"))
    renewing: list[bool] = [False]
    closed_while_renewing: list[bool] = []

    @contextlib.contextmanager
    def _renewing(_worker_id: int) -> Iterator[None]:
        """Record that a lease renewal block is open."""
        renewing[0] = True
        try:
            yield
        finally:
            renewing[0] = False

    def _close(*_args: Any) -> tuple[dict[str, Any], None]:
        """Record whether the lease renews while the gateway closes."""
        closed_while_renewing.append(renewing[0])
        return {}, None

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch("core.worker.engine.bind_protected_sandbox"),
        patch("core.worker.engine.payload_uses_token_source", return_value=False),
        patch("core.worker.engine.ModelGateway", FakeGateway),
        patch("core.worker.engine.mp.get_context", return_value=context),
        patch("core.worker.engine.validate_blackbox_payload"),
        patch.object(worker, "_renewing_lease", _renewing),
        patch.object(worker, "_close_budget_gateway", _close),
        patch.object(worker, "_schedule_embedding_indexing"),
    ):
        worker._process_job("lease-close", 0)

    assert closed_while_renewing == [True]


def test_unprotected_stored_job_is_rejected_before_host_execution() -> None:
    """Require old stored jobs to be resubmitted through protected admission."""
    store = FakeJobStore()
    store.seed_job(
        "legacy-dspy",
        payload=dict(REAL_RUN_PAYLOAD),
        payload_overview={
            "optimization_type": OPTIMIZATION_TYPE_BLACKBOX,
            "username": "alice",
            "token_source": "managed",
        },
    )
    worker = engine.BackgroundWorker(job_store=cast(JobStore, store), num_workers=1)
    worker.enqueue_job("legacy-dspy")
    context, _process = make_mp_context(result_events=[make_result_event()])
    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch("core.worker.engine.mp.get_context", return_value=context),
        patch("core.worker.engine.validate_blackbox_payload") as validate,
        patch.object(worker, "_schedule_embedding_indexing"),
    ):
        worker._process_job("legacy-dspy", 0)

    validate.assert_not_called()
    context.Process.assert_not_called()
    job = store.get_job("legacy-dspy")
    assert job["status"] == "failed"
    assert "Submit it again" in job["message"]


def test_worker_rejects_dirty_credentials_before_guest_dispatch() -> None:
    """Fail a dirty stored row without copying its credential into a child process."""
    payload = copy.deepcopy(REAL_RUN_PAYLOAD)
    payload["reflection_model_config"] = {
        **payload["reflection_model_config"],
        "extra": {"nested": {"Authorization": "Bearer legacy-dirty-secret"}},
    }
    store = FakeJobStore()
    store.seed_job(
        "legacy-dirty",
        payload=payload,
        payload_overview={
            "optimization_type": OPTIMIZATION_TYPE_BLACKBOX,
            "username": "alice",
            "token_source": "managed",
        },
    )
    worker = engine.BackgroundWorker(job_store=cast(JobStore, store), num_workers=1)
    worker.enqueue_job("legacy-dirty")
    context, _process = make_mp_context(result_events=[make_result_event()])
    protect_dispatch(store, context)

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch("core.worker.engine.validate_blackbox_payload"),
    ):
        worker._process_job("legacy-dirty", 0)

    job = store.get_job("legacy-dirty")
    assert job["status"] == "failed"
    assert "legacy-dirty-secret" not in str(job.get("message"))
    assert "outside the trusted relay" in job["message"]
    context.Process.assert_not_called()

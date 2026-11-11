"""Verify protected DSPy jobs cross the selected sandbox before authored code runs."""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from core.constants import OPTIMIZATION_TYPE_RUN
from core.storage.base import JobStore
from core.worker import engine

from .conftest import FakeJobStore
from .mocks import REAL_RUN_PAYLOAD, REAL_UNIFIED_PAYLOAD, make_mp_context, make_result_event


class _Gateway:
    """Retain the protected payload without starting real parent transports."""

    def __init__(self, runtime: Any, **_kwargs: Any) -> None:
        """Store the budget runtime supplied by the worker."""
        self.runtime = runtime

    def protect_payload(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        """Return the same authored source for assertion at the sandbox boundary."""
        return {**payload, "_protected_fixture": True}

    def detach_sandbox(self) -> None:
        """Report that no sandbox broker is bound to this fixture."""

    def close(self) -> None:
        """Close the transport fixture without external resources."""


def _payload(marker: Path, runtime: str) -> dict[str, Any]:
    """Build structurally valid workflow source with observable parent side effects.

    Args:
        marker: Host file written if any module scope executes before the sandbox.
        runtime: Selected outer execution environment.

    Returns:
        A protected workflow payload accepted by the request schema.
    """
    side_effect = (
        "import os\n"
        "import pathlib\n"
        "import socket\n"
        f"pathlib.Path({str(marker)!r}).write_text(os.environ.get('APP_PARENT_ONLY_SECRET', 'missing'))\n"
        "socket.create_connection(('127.0.0.1', 1), timeout=0.1)\n"
    )
    return {
        "username": "alice",
        "module_name": "workflow",
        "workflow": {
            "nodes": [
                {"id": "input", "kind": "input", "fields": [{"name": "question"}]},
                {
                    "id": "predict",
                    "kind": "signature",
                    "signature_code": side_effect
                    + "class Sig(dspy.Signature):\n"
                    + "    question: str = dspy.InputField()\n"
                    + "    draft: str = dspy.OutputField()\n",
                },
                {
                    "id": "reshape",
                    "kind": "transform",
                    "transform_code": side_effect + "def transform(draft): return {'answer': draft}\n",
                    "input_fields": [{"name": "draft"}],
                    "output_fields": [{"name": "answer"}],
                },
                {"id": "output", "kind": "output", "fields": [{"name": "answer"}]},
            ],
            "edges": [
                {"source": "input", "source_port": "question", "target": "predict", "target_port": "question"},
                {"source": "predict", "source_port": "draft", "target": "reshape", "target_port": "draft"},
                {"source": "reshape", "source_port": "answer", "target": "output", "target_port": "answer"},
            ],
        },
        "metric_code": side_effect
        + "def metric(gold, pred, trace=None, pred_name=None, pred_trace=None): return 1.0\n",
        "optimizer_name": "gepa",
        "dataset": [{"question": "Q?", "answer": "A"}],
        "column_mapping": {"inputs": {"question": "question"}, "outputs": {"answer": "answer"}},
        "model_config": {"name": "fixture/task"},
        "reflection_model_config": {"name": "fixture/reflection"},
        "execution_runtime": runtime,
        "execution_budget_id": "budget",
        "execution_budget_revision": 1,
        "execution_budget_generation": 0,
        "max_cost_cents": 20,
    }


@pytest.mark.parametrize(
    "runtime",
    ["worker", "vercel"],
)
def test_worker_orchestrator_skips_host_exec_and_dispatches_authored_source_to_outer_sandbox(
    runtime: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep all semantic validation in the selected protected executor.

    Args:
        runtime: Current or retired payload value, both canonicalized to Vercel.
        tmp_path: Host-only marker path authored code must not reach.
        monkeypatch: Parent credential and runtime fixture.
    """
    marker = tmp_path / "worker-parent-side-effect"
    payload = _payload(marker, runtime)
    store = FakeJobStore()
    store.engine = object()
    store.seed_job(
        "protected-dspy",
        payload=payload,
        payload_overview={
            "optimization_type": OPTIMIZATION_TYPE_RUN,
            "username": "alice",
            "token_source": "managed",
        },
        execution_budget_id="budget",
        execution_budget_generation=0,
    )
    worker = engine.BackgroundWorker(job_store=cast(JobStore, store), num_workers=1)
    worker.enqueue_job("protected-dspy")
    context, _process = make_mp_context(result_events=[make_result_event()])
    service = MagicMock()
    monkeypatch.setenv("APP_PARENT_ONLY_SECRET", "must-stay-in-parent")
    monkeypatch.setattr(engine.settings, "openrouter_api_key", SecretStr("fixture-only"))

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch("core.worker.engine.bind_protected_sandbox"),
        patch("core.worker.engine.payload_uses_token_source", return_value=False),
        patch("core.worker.engine.ModelGateway", _Gateway),
        patch("core.worker.engine.mp.get_context", return_value=context),
        patch.object(worker, "_get_service", return_value=service),
        patch.object(worker, "_close_budget_gateway", return_value=({}, None)),
        patch.object(worker, "_schedule_embedding_indexing"),
    ):
        worker._process_job("protected-dspy", 0)

    service.validate_payload.assert_not_called()
    assert not marker.exists()
    assert store.get_job("protected-dspy")["status"] == "success"
    launch = context.Process.call_args.kwargs
    assert launch["target"] is engine.run_vercel_dspy
    launched_payload = launch["args"][0]
    assert launched_payload["_protected_fixture"] is True
    assert launched_payload["workflow"] == payload["workflow"]
    assert launched_payload["metric_code"] == payload["metric_code"]


def test_unified_byok_roles_are_aliased_before_worker_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep all unified model aliases available through BYOK injection and protection."""
    payload = copy.deepcopy(REAL_UNIFIED_PAYLOAD)
    payload["models"] = {
        "task_model": {"name": "fixture/task"},
        "reflection_model": {"name": "fixture/reflection"},
        "metric_model": {"name": "openai/judge", "token_source": "byok", "byok_provider": "openai"},
    }
    payload["max_cost_cents"] = 20
    store = FakeJobStore()
    store.engine = object()
    store.seed_job(
        "unified-byok",
        payload=payload,
        payload_overview={"optimization_type": "blackbox", "username": "alice", "token_source": "managed"},
        execution_budget_id="budget",
        execution_budget_generation=0,
    )
    worker = engine.BackgroundWorker(job_store=cast(JobStore, store), num_workers=1)
    worker.enqueue_job("unified-byok")
    context, _process = make_mp_context(result_events=[make_result_event()])
    monkeypatch.setattr(engine.settings, "openrouter_api_key", SecretStr("fixture-only"))
    events: list[str] = []
    alias = engine.alias_unified_model_roles

    def alias_spy(source: dict[str, Any]) -> None:
        """Record when the worker adds the legacy model aliases."""
        events.append("alias")
        alias(source)

    def inject(source: dict[str, Any], **kwargs: Any) -> None:
        """Stand in for vault resolution after checking the BYOK metric alias."""
        events.append("inject")
        assert source["scorer"]["model"] is source["models"]["metric_model"]
        source["scorer"]["model"].setdefault("extra", {})["api_key"] = "metric-private"

    class _RoutingGateway(_Gateway):
        """Check the exact worker-to-protection boundary."""

        def protect_payload(self, source: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            """Check each aliased model and the resolved metric credential."""
            events.append("protect")
            assert source["task_model_config"] is source["models"]["task_model"]
            assert source["reflection_model_config"] is source["models"]["reflection_model"]
            metric = source["scorer"]["model"]
            assert metric is source["models"]["metric_model"]
            assert metric["token_source"] == "byok"
            assert metric["extra"]["api_key"] == "metric-private"
            result = copy.deepcopy(source)
            result["scorer"]["model"]["extra"].pop("api_key")
            return result

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch("core.worker.engine.bind_protected_sandbox"),
        patch("core.worker.engine.validate_unified_payload"),
        patch("core.worker.engine.OpenRouterKeyProvisioner", return_value=MagicMock(enabled=False)),
        patch("core.worker.engine.alias_unified_model_roles", side_effect=alias_spy),
        patch("core.worker.engine.inject_byok_connections", side_effect=inject),
        patch("core.worker.engine.ModelGateway", _RoutingGateway),
        patch("core.worker.engine.mp.get_context", return_value=context),
        patch.object(worker, "_get_service", return_value=MagicMock()),
        patch.object(worker, "_close_budget_gateway", return_value=({}, None)),
        patch.object(worker, "_schedule_embedding_indexing"),
    ):
        worker._process_job("unified-byok", 0)

    assert events == ["alias", "inject", "protect"]
    assert store.get_job("unified-byok")["status"] == "success"
    launched_payload = context.Process.call_args.kwargs["args"][0]
    assert not {"scorer", "task_model_config", "reflection_model_config"} & launched_payload.keys()


def test_budget_gateway_closes_while_the_lease_renews(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the claim alive while the gateway waits for in-flight usage, so the sweep cannot race it.

    Args:
        monkeypatch: Parent credential fixture.
        tmp_path: Host-only marker path for the authored fixture.
    """
    store = FakeJobStore()
    store.engine = object()
    store.seed_job(
        "lease-close",
        payload=_payload(tmp_path / "marker", "vercel"),
        payload_overview={"optimization_type": OPTIMIZATION_TYPE_RUN, "username": "alice", "token_source": "managed"},
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
        patch("core.worker.engine.ModelGateway", _Gateway),
        patch("core.worker.engine.mp.get_context", return_value=context),
        patch.object(worker, "_get_service", return_value=MagicMock()),
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
            "optimization_type": OPTIMIZATION_TYPE_RUN,
            "username": "alice",
            "token_source": "managed",
        },
    )
    worker = engine.BackgroundWorker(job_store=cast(JobStore, store), num_workers=1)
    worker.enqueue_job("legacy-dspy")
    context, _process = make_mp_context(result_events=[make_result_event()])
    worker._mp_ctx = context
    worker._mp_start_method = "spawn"
    service = MagicMock()

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch.object(worker, "_get_service", return_value=service),
        patch.object(worker, "_schedule_embedding_indexing"),
    ):
        worker._process_job("legacy-dspy", 0)

    service.validate_payload.assert_not_called()
    context.Process.assert_not_called()
    job = store.get_job("legacy-dspy")
    assert job["status"] == "failed"
    assert "Submit it again" in job["message"]


def test_legacy_worker_rejects_dirty_credentials_before_guest_dispatch() -> None:
    """Fail a dirty legacy row without copying its credential into a child process."""
    payload = copy.deepcopy(REAL_RUN_PAYLOAD)
    payload["model_settings"]["extra"] = {"nested": {"Authorization": "Bearer legacy-dirty-secret"}}
    store = FakeJobStore()
    store.seed_job(
        "legacy-dirty",
        payload=payload,
        payload_overview={
            "optimization_type": OPTIMIZATION_TYPE_RUN,
            "username": "alice",
            "token_source": "managed",
        },
    )
    worker = engine.BackgroundWorker(
        job_store=cast(JobStore, store),
        num_workers=1,
        _allow_unprotected_test_execution=True,
    )
    worker.enqueue_job("legacy-dirty")
    context, _process = make_mp_context(result_events=[make_result_event()])
    worker._mp_ctx = context
    worker._mp_start_method = "spawn"

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch.object(worker, "_get_service", return_value=MagicMock()),
    ):
        worker._process_job("legacy-dirty", 0)

    job = store.get_job("legacy-dirty")
    assert job["status"] == "failed"
    assert "legacy-dirty-secret" not in str(job.get("message"))
    context.Process.assert_not_called()

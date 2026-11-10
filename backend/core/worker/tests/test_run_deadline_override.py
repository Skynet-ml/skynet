"""Tests for the per-run deadline override on a repository run (SKY-572)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from core.billing.protected_execution import bind_protected_sandbox
from core.billing.tests.test_protected_execution_local import _Gateway, local_settings  # noqa: F401
from core.config import Settings
from core.models.blackbox import BlackboxRunRequest
from core.service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY
from core.storage.base import JobStore

from .. import engine as engine_module
from ..engine import BackgroundWorker, reset_worker_for_tests
from .conftest import FakeJobStore
from .mocks import make_mp_context, make_result_event

_OVERVIEW = {"optimization_type": "blackbox", "username": "alice"}


@pytest.fixture(autouse=True)
def _reset_global_worker() -> Iterator[None]:
    """Reset the module-level worker singleton around each test."""
    reset_worker_for_tests()
    yield
    reset_worker_for_tests()


def _request(**extra: Any) -> BlackboxRunRequest:
    """Build a minimal repository run request.

    Args:
        **extra: More request fields.

    Returns:
        The validated request.
    """
    return BlackboxRunRequest(
        username="alice",
        recipe="repo",
        scorer={"kind": "python", "metric_code": "def score(candidate, case=None): return 1.0"},
        target={"kind": "repo", "repo": {"repository": "acme/app", "editable_paths": ["src"]}},
        reflection_model_config={"name": "gpt-4o"},
        **extra,
    )


def _bound_lifetime(payload: dict[str, Any]) -> Any:
    """Run one stored repository job and return the lifetime it binds its sandbox with.

    Args:
        payload: The stored run request.

    Returns:
        The ``lifetime_seconds`` argument the engine passed to the sandbox bind.
    """
    store = FakeJobStore()
    store.engine = MagicMock()
    store.seed_job(
        "repo-deadline",
        payload=payload,
        payload_overview=_OVERVIEW,
        execution_budget_id="budget",
        execution_budget_generation=0,
    )
    worker = BackgroundWorker(
        job_store=cast(JobStore, store),
        num_workers=1,
        poll_interval=1.0,
        _allow_unprotected_test_execution=True,
    )
    worker.enqueue_job("repo-deadline")
    gateway = MagicMock()
    gateway.protect_payload.side_effect = lambda payload, **kwargs: payload
    gateway.cost_ceiling_cents.return_value = 100

    def stage(payload: dict, **kwargs: object) -> tuple[dict, MagicMock]:
        """Stage the repository the way the real step leaves the payload."""
        payload["target"]["repo"][engine_module.ENVIRONMENT_KEY] = {}
        payload[REPO_SNAPSHOT_KEY] = {}
        return payload, MagicMock()

    ctx, _proc = make_mp_context(exitcode=0, result_events=[make_result_event()])
    with contextlib.ExitStack() as stack:
        for target, kwargs in (
            ("notify_job_completed", {}),
            ("record_server_event", {}),
            ("validate_blackbox_payload", {}),
            ("payload_uses_token_source", {"return_value": False}),
            ("ModelGateway", {"return_value": gateway}),
            ("BudgetService", {}),
            ("BudgetRuntime", {}),
            ("protected_image", {"return_value": None}),
            ("resolve_execution_credentials", {"side_effect": lambda payload, **kwargs: payload}),
            ("host_proposer", {"return_value": None}),
            ("stage_repository", {"side_effect": stage}),
            ("check_repository", {"return_value": []}),
            ("infer_repo_setup", {"return_value": None}),
            ("bind_repo_scorer", {}),
            ("has_exposed_execution_credentials", {"return_value": False}),
            ("publish_improvement", {}),
        ):
            stack.enter_context(patch(f"core.worker.engine.{target}", **kwargs))
        stack.enter_context(patch("core.worker.engine.mp.get_context", return_value=ctx))
        bind = stack.enter_context(patch("core.worker.engine.bind_protected_sandbox"))
        stack.enter_context(patch.object(worker, "_get_service"))
        stack.enter_context(patch.object(worker, "_record_repo_commit"))
        stack.enter_context(patch.object(worker, "_record_repo_environment", return_value=None))
        stack.enter_context(patch.object(worker, "_close_budget_gateway", return_value=(None, None)))
        worker._process_job("repo-deadline", 0)
    bind.assert_called_once()
    return bind.call_args.kwargs["lifetime_seconds"]


def test_stored_run_binds_its_deadline_override() -> None:
    """A run that asks for a 120 s deadline binds its sandbox with 120 s."""
    payload = _request(deadline_seconds=120).model_dump(mode="json", by_alias=True)

    assert payload["deadline_seconds"] == 120
    assert _bound_lifetime(payload) == 120


def test_run_without_override_keeps_the_configured_deadline() -> None:
    """A run without the field passes no override, so the configured lifetime applies."""
    assert _bound_lifetime(_request().model_dump(mode="json", by_alias=True)) is None


def test_override_below_one_minute_is_refused() -> None:
    """The request refuses a deadline shorter than 60 s."""
    with pytest.raises(ValidationError):
        _request(deadline_seconds=59)


def test_override_only_shortens_the_configured_deadline(
    local_settings: tuple[Settings, Path],  # noqa: F811
) -> None:
    """The bind uses a shorter override and caps a longer one at the configured lifetime."""
    settings, _ = local_settings
    configured = settings.vercel_sandbox_max_lifetime_seconds

    short = bind_protected_sandbox(_Gateway(), settings, workflow="anything", owner_id="job-1", lifetime_seconds=120)
    long = bind_protected_sandbox(
        _Gateway(), settings, workflow="anything", owner_id="job-2", lifetime_seconds=configured + 1000
    )
    default = bind_protected_sandbox(_Gateway(), settings, workflow="anything", owner_id="job-3")

    assert short["lifetime_seconds"] == 120
    assert long["lifetime_seconds"] == default["lifetime_seconds"] == configured

"""Verify protected Anything runs place the complete optimizer in one selected sandbox."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from core.api import preflight_execution
from core.constants import OPTIMIZATION_TYPE_BLACKBOX
from core.service_gateway.optimization.blackbox.sandbox import (
    ContainedSubprocessRuntime,
    SandboxSpec,
    current_sandbox_runtime,
)
from core.worker import engine, isolated_runner

from .conftest import FakeJobStore


def test_anything_guest_binds_contained_runtime_around_optimizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep nested scorer, target, and native commands inside the optimizer's outer sandbox.

    Args:
        tmp_path: Guest request staging directory.
        monkeypatch: Runner and argument substitutions.
    """
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "payload": {"_optimization_type": OPTIMIZATION_TYPE_BLACKBOX},
                "artifact_id": "anything-g1",
                "nonce": "fixture",
                "protocol": isolated_runner.SANDBOX_PROTOCOL,
            }
        )
    )
    observed: list[Any] = []

    def run(payload: dict[str, Any], artifact_id: str, events: Any, start_method: str) -> None:
        """Capture the runtime visible at the optimizer entry point."""
        observed.append(current_sandbox_runtime())

    monkeypatch.setattr(isolated_runner, "run_service_in_subprocess", run)
    monkeypatch.setattr(isolated_runner.sys, "argv", ["isolated_runner", str(request)])
    isolated_runner.main()

    assert len(observed) == 1
    assert isinstance(observed[0], ContainedSubprocessRuntime)
    assert observed[0].protected is True
    assert current_sandbox_runtime() is None


@pytest.mark.parametrize("runtime", ["worker", "vercel"])
def test_anything_continue_uses_one_selected_outer_supervisor(runtime: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Send each Continue check through one outer sandbox invocation.

    Args:
        runtime: Current or retired payload value, both executed in Vercel.
        monkeypatch: Selected supervisor substitutions.
    """
    calls: list[tuple[dict[str, Any], str]] = []

    def execute(payload: dict[str, Any], artifact_id: str, events: Any, _start_method: str) -> None:
        """Record the one outer setup invocation and return its guest evidence."""
        calls.append((payload, artifact_id))
        events.put({"type": "preflight_result", "result": {"checks": []}})

    monkeypatch.setattr(preflight_execution, "run_vercel_dspy", execute)
    result = preflight_execution._verify_anything(
        object(),
        {"proposer_runtime": runtime},
        scope="evaluation",
        identity="setup-one",
    )

    assert result == {"checks": []}
    assert len(calls) == 1
    payload, artifact_id = calls[0]
    assert payload["_optimization_type"] == OPTIMIZATION_TYPE_BLACKBOX
    assert payload["_preflight"] == {"scope": "evaluation", "identity": "setup-one"}
    assert artifact_id == "preflight-setup-one"


def test_anything_guest_binds_contained_runtime_around_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run readiness orchestration inside the outer sandbox instead of the API parent.

    Args:
        tmp_path: Guest request staging directory.
        monkeypatch: Preflight and argument substitutions.
    """
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "payload": {
                    "_optimization_type": OPTIMIZATION_TYPE_BLACKBOX,
                    "_preflight": {"scope": "evaluation", "identity": "inside"},
                },
                "artifact_id": "preflight-inside",
                "nonce": "fixture",
                "protocol": isolated_runner.SANDBOX_PROTOCOL,
            }
        )
    )
    observed: list[Any] = []

    def verify(payload: dict[str, Any], *, scope: str, identity: str, runtime: Any) -> dict[str, Any]:
        """Capture the runtime enclosing the guest preflight implementation."""
        observed.append((payload, scope, identity, runtime, current_sandbox_runtime()))
        return {"checks": []}

    def unexpected(*_args: Any, **_kwargs: Any) -> None:
        """Fail if the host-style service runner handles setup."""
        raise AssertionError("Anything preflight reached the host-style service runner")

    monkeypatch.setattr(isolated_runner, "verify_anything_in_sandbox", verify)
    monkeypatch.setattr(isolated_runner, "run_service_in_subprocess", unexpected)
    monkeypatch.setattr(isolated_runner.sys, "argv", ["isolated_runner", str(request)])
    isolated_runner.main()

    assert len(observed) == 1
    payload, scope, identity, runtime, active = observed[0]
    assert payload["_optimization_type"] == OPTIMIZATION_TYPE_BLACKBOX
    assert scope == "evaluation"
    assert identity == "inside"
    assert isinstance(runtime, ContainedSubprocessRuntime)
    assert active is runtime
    assert current_sandbox_runtime() is None


def test_pending_guest_preflight_does_not_run_parent_model_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve a pending evaluator result without spending on later role checks.

    Args:
        monkeypatch: Outer supervisor substitution.
    """

    def execute(_payload: dict[str, Any], _artifact_id: str, events: Any, _start_method: str) -> None:
        """Return the same early pending evidence emitted by the guest verifier."""
        events.put(
            {
                "type": "preflight_result",
                "result": {"checks": [{"key": "scorer.model", "status": "pending"}]},
            }
        )

    class Gateway:
        """Reject any parent model check after incomplete guest readiness."""

        def model_routes(self) -> list[dict[str, Any]]:
            """Fail if later checks run before the scorer configuration is complete."""
            raise AssertionError("model readiness ran after a pending scorer check")

    monkeypatch.setattr(preflight_execution, "run_vercel_dspy", execute)
    result = preflight_execution._verify_anything(
        Gateway(),
        {"proposer_runtime": "worker"},
        scope="execution",
        identity="pending",
    )
    assert result == {"checks": [{"key": "scorer.model", "status": "pending"}]}


def test_protected_parent_does_not_load_scorer_code_before_sandbox() -> None:
    """Limit parent validation to structure before the outer sandbox starts."""
    store = FakeJobStore()
    store.seed_job(
        "protected-anything",
        payload={
            "name": "fixture",
            "username": "alice",
            "seed_candidate": "seed",
            "scorer": {"kind": "python", "metric_code": "def score(candidate): return 1.0"},
            "reflection_model_config": {"name": "fixture/model"},
        },
        payload_overview={
            "optimization_type": OPTIMIZATION_TYPE_BLACKBOX,
            "username": "alice",
            "token_source": "managed",
        },
        execution_budget_id="budget",
        execution_budget_generation=0,
    )
    worker = engine.BackgroundWorker(store, num_workers=1)
    worker.enqueue_job("protected-anything")
    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.validate_blackbox_payload") as validate,
        patch.object(worker, "_get_service"),
    ):
        worker._process_job("protected-anything", 0)

    assert validate.call_count == 1
    assert validate.call_args.kwargs == {"verify_scorer": False, "api_bases": {}}


def test_contained_commands_cannot_restore_provider_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force nested commands back through the outer mailbox even when they supply an endpoint.

    Args:
        monkeypatch: Outer mailbox environment substitution.
    """
    relay = "http://127.0.0.1:43123/v1"
    monkeypatch.setenv("SKYNET_BUDGET_RELAY_URL", relay)
    runtime = ContainedSubprocessRuntime()
    session = runtime.open(SandboxSpec(lifetime_seconds=10))
    try:
        session.write_files(
            {
                "inspect_env.py": (
                    "import json, os\n"
                    "names = ('SKYNET_BUDGET_RELAY_URL', 'SKYNET_GATEWAY_URL', "
                    "'OPENAI_BASE_URL', 'ANTHROPIC_BASE_URL')\n"
                    "print(json.dumps({name: os.environ[name] for name in names}))\n"
                )
            }
        )
        result = session.run(
            "python3 inspect_env.py",
            env={
                "OPENAI_BASE_URL": "https://provider.invalid/v1",
                "ANTHROPIC_BASE_URL": "https://provider.invalid",
            },
            timeout_seconds=5,
        )
    finally:
        session.close()
    assert result.ok
    environment = json.loads(result.stdout)
    assert environment == {
        "SKYNET_BUDGET_RELAY_URL": relay,
        "SKYNET_GATEWAY_URL": relay,
        "OPENAI_BASE_URL": relay,
        "ANTHROPIC_BASE_URL": relay.removesuffix("/v1"),
    }

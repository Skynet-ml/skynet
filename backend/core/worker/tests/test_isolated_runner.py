"""Verify the guest drops its request file and runs black-box jobs in the contained runtime."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing.budgets import BudgetInsufficientError
from core.worker import isolated_runner

SENTINEL = "METRIC_SENTINEL_b81e"


def _write_request(path: Path, payload: dict[str, Any]) -> None:
    """Stage a guest request the way the supervisor does.

    Args:
        path: Request file to write.
        payload: The run payload.
    """
    path.write_text(
        json.dumps(
            {"payload": payload, "artifact_id": "g1", "nonce": "fixture", "protocol": isolated_runner.SANDBOX_PROTOCOL}
        )
    )


def test_request_file_is_gone_once_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the metric in memory only, and leave no request file a proposer could read.

    Args:
        tmp_path: Guest request staging directory.
        monkeypatch: Runner and argument substitutions.
    """
    request = tmp_path / "request.json"
    payload = {"_optimization_type": "blackbox", "workspace": {"metric_code": f"# {SENTINEL}"}}
    _write_request(request, payload)
    observed: list[dict[str, Any]] = []

    def run(received: dict[str, Any], artifact_id: str, events: Any, start_method: str) -> None:
        """Record the payload the optimizer entry point receives."""
        assert not request.exists()
        observed.append(received)

    monkeypatch.setattr(isolated_runner, "run_service_in_subprocess", run)
    monkeypatch.setattr(isolated_runner, "ContainedSubprocessRuntime", object)
    monkeypatch.setattr(isolated_runner, "sandbox_runtime_context", lambda _runtime: contextlib.nullcontext())
    monkeypatch.setattr(isolated_runner.sys, "argv", ["isolated_runner", str(request)])
    isolated_runner.main()

    assert len(observed) == 1
    assert observed[0]["workspace"]["metric_code"] == f"# {SENTINEL}"
    assert not request.exists()
    assert all(SENTINEL not in path.read_text() for path in tmp_path.rglob("*") if path.is_file())


def test_blackbox_payloads_run_in_the_contained_runtime() -> None:
    """Treat only black-box payloads as contained runs."""
    assert isolated_runner._is_blackbox_family({"_optimization_type": "blackbox"})
    assert not isolated_runner._is_blackbox_family({"_optimization_type": "run"})


def test_a_failed_preflight_names_its_error_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tag the error event with the exception type, so the parent can tell a budget stop from broken code.

    Args:
        tmp_path: Guest request staging directory.
        monkeypatch: Verifier, stream and argument substitutions.
    """
    request = tmp_path / "request.json"
    document = {
        "payload": {"_optimization_type": "blackbox", "_preflight": {"scope": "evaluation", "identity": "i"}},
        "artifact_id": "g1",
        "nonce": "fixture",
        "protocol": isolated_runner.SANDBOX_PROTOCOL,
    }
    request.write_text(json.dumps(document))
    written: list[str] = []

    def spend(payload: dict[str, Any], preflight: dict[str, Any], events: Any) -> dict[str, Any]:
        """Fail the way a model call's hold over the limit does inside the guest."""
        raise BudgetInsufficientError("The next operation needs up to 135 cents.")

    monkeypatch.setattr(isolated_runner, "_run_preflight", spend)
    monkeypatch.setattr(isolated_runner, "ContainedSubprocessRuntime", object)
    monkeypatch.setattr(isolated_runner, "sandbox_runtime_context", lambda _runtime: contextlib.nullcontext())
    monkeypatch.setattr(isolated_runner.sys, "__stdout__", SimpleNamespace(write=written.append, flush=lambda: None))
    monkeypatch.setattr(isolated_runner.sys, "argv", ["isolated_runner", str(request)])
    isolated_runner.main()

    [event] = [json.loads(line.partition("fixture ")[2]) for line in written if line.strip()]
    assert (event["type"], event["error_type"]) == ("error", "BudgetInsufficientError")

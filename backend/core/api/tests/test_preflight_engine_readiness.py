"""Check that the Continue check refuses an engine that this worker image cannot run (SKY-728)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.api import preflight_execution
from core.billing.model_dispatch import ModelHTTPResult
from core.models import BlackboxRunRequest
from core.service_gateway.optimization.blackbox import native_runtime

_READY = {"autosaddler_ready": True, "shinka_ready": True}


def _request(strategy: dict[str, Any]) -> BlackboxRunRequest:
    """Build a valid repository run request with the given strategy.

    Args:
        strategy: The ``strategy`` field of the request.

    Returns:
        The validated request.
    """
    return BlackboxRunRequest.model_validate(
        {
            "recipe": "repo",
            "scorer": {"kind": "python", "metric_code": "def score(repo_path):\n    return 1.0\n"},
            "reflection_model_config": {"name": "gpt-4o"},
            "budget": {"max_evals": 10},
            "strategy": strategy,
            "target": {
                "kind": "repo",
                "repo": {"repository": "octo/hello", "branch": "main", "editable_paths": ["src/"]},
            },
        }
    )


@pytest.mark.parametrize(
    ("engine", "flag", "package"),
    [("shinka_evolve", "shinka_ready", "ShinkaEvolve"), ("autosaddler", "autosaddler_ready", "AutoSaddler")],
)
def test_a_single_run_fails_when_its_engine_interpreter_is_missing(
    engine: str, flag: str, package: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail the optimizer check of a single run whose engine has no interpreter in the image.

    Args:
        engine: The single engine of the run.
        flag: The readiness key that is false.
        package: The package name that the message names.
        monkeypatch: Replaces the image probe.
    """
    monkeypatch.setattr(preflight_execution, "check_native_runtime", lambda: {**_READY, flag: False})

    check = preflight_execution._engine_readiness(_request({"mode": "single", "engine": engine}))

    assert check["key"] == "optimizer"
    assert check["status"] == "failed"
    assert package in check["message"]


@pytest.mark.parametrize("flag", ["shinka_ready", "autosaddler_ready"])
def test_auto_passes_when_one_engine_interpreter_is_missing(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass Auto, which only loses that engine's lane.

    Args:
        flag: The readiness key that is false.
        monkeypatch: Replaces the image probe.
    """
    monkeypatch.setattr(preflight_execution, "check_native_runtime", lambda: {**_READY, flag: False})

    check = preflight_execution._engine_readiness(_request({"mode": "auto"}))

    assert (check["key"], check["status"]) == ("optimizer", "succeeded")


def test_a_wrong_gepa_commit_fails_the_optimizer_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the image probe's GEPA commit error into a failed optimizer check.

    Args:
        monkeypatch: Replaces the installed GEPA's provenance.
    """
    wrong = SimpleNamespace(read_text=lambda _name: '{"vcs_info": {"commit_id": "0000000"}}')
    monkeypatch.setattr(native_runtime, "distribution", lambda _name: wrong)

    check = preflight_execution._engine_readiness(_request({"mode": "single", "engine": "gepa"}))

    assert check["status"] == "failed"
    assert native_runtime.GEPA_SOURCE in check["message"]


class _Gateway:
    """Answer every model route check with success."""

    def model_routes(self) -> list[dict[str, Any]]:
        """Return one optimization route.

        Returns:
            The routes of the run.
        """
        return [{"role": "optimization", "model": "fixture/model", "token": "route"}]

    def dispatch_guest(self, _token: str, _path: str, _body: dict[str, Any], _headers: dict[str, str]) -> Any:
        """Answer the setup request.

        Returns:
            A successful response.
        """
        return SimpleNamespace(status=200)


def test_model_route_checks_run_before_the_engine_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the model route checks ahead of a failed engine check in the Continue result.

    Args:
        monkeypatch: Replaces the setup box and the image probe.
    """

    def execute(_payload: dict[str, Any], _artifact_id: str, events: Any, _start_method: str) -> None:
        """Return the setup box's evidence with a passed optimizer check."""
        events.put({"type": "preflight_result", "result": {"checks": [{"key": "optimizer", "status": "succeeded"}]}})

    monkeypatch.setattr(preflight_execution, "run_vercel_dspy", execute)
    monkeypatch.setattr(preflight_execution, "check_native_runtime", lambda: {**_READY, "shinka_ready": False})
    payload = _request({"mode": "single", "engine": "shinka_evolve"}).model_dump(mode="json", by_alias=True)

    result = preflight_execution._verify_anything(_Gateway(), payload, scope="execution", identity="engine")

    assert [(check["key"], check["status"]) for check in result["checks"]] == [
        ("model.optimization", "succeeded"),
        ("optimizer", "failed"),
    ]


_ROUTE_TOKEN = "route-token-for-the-check"


class _RateLimitedGateway(_Gateway):
    """Answer every model route check with OpenRouter's rate limit refusal."""

    def model_routes(self) -> list[dict[str, str]]:
        """Return one route whose token the refusal echoes.

        Returns:
            The optimization route.
        """
        return [{"role": "optimization", "model": "fixture/model", "token": _ROUTE_TOKEN}]

    def dispatch_guest(self, _token: str, _path: str, _body: dict[str, Any], _headers: dict[str, str]) -> Any:
        """Refuse the setup request as OpenRouter does, with a message over two lines that echoes the token.

        Returns:
            A 429 response with the provider's message.
        """
        message = f"Rate limit exceeded:\n  free-models-per-min ({_ROUTE_TOKEN})"
        body = json.dumps({"error": {"message": message, "code": 429}}).encode()
        return ModelHTTPResult(status=429, content_type="application/json", body=body)


def test_a_model_route_refusal_shows_the_provider_message() -> None:
    """The setup refusal reads the provider's own reason from the body, on one line and with no token (SKY-924)."""
    [check] = preflight_execution._verify_model_routes(_RateLimitedGateway(), native=False)

    assert check["status"] == "failed"
    assert "(HTTP 429: Rate limit exceeded: free-models-per-min ([key]))." in check["message"]
    assert _ROUTE_TOKEN not in check["message"]

"""Keep the setup check's model calls unbatched, even for an economy-marked model (SKY-284)."""

from types import SimpleNamespace
from typing import Any

import pytest

from core.api import preflight_execution
from core.api.preflight_execution import WizardPreflightRequest, _perform_preflight

SCORER = {"kind": "python", "metric_code": "def metric(candidate, case):\n    return 1.0\n"}
PASSED = {"checks": [{"key": "scorer.readiness", "status": "succeeded"}]}


def test_setup_check_forbids_a_batched_model_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send ``allow_batch=False`` to the gateway when the payload asks for economy."""
    options: list[dict[str, Any]] = []

    class _RecordingGateway:
        """Record the protection options the setup check sends, without paid transport."""

        def __init__(self, runtime: Any) -> None:
            """Keep the runtime the preflight builds the gateway with.

            Args:
                runtime: The gateway runtime the preflight passes in.
            """
            self.runtime = runtime

        def protect_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            """Record the options and hand the payload back unchanged.

            Args:
                payload: The payload the preflight protects.
                **kwargs: The protection options under test.

            Returns:
                The same payload.
            """
            options.append(kwargs)
            return payload

        def close(self) -> None:
            """Nothing to settle."""

    monkeypatch.setattr(preflight_execution, "ModelGateway", _RecordingGateway)
    monkeypatch.setattr(
        preflight_execution,
        "bind_protected_sandbox",
        lambda gateway, settings, **kwargs: {"image": "image", "lifetime_seconds": kwargs["lifetime_seconds"]},
    )
    monkeypatch.setattr(preflight_execution, "payload_uses_token_source", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        preflight_execution, "_verify_anything", lambda gateway, payload, *, scope, identity: dict(PASSED)
    )
    request = WizardPreflightRequest(
        scope="evaluation",
        workflow="anything",
        payload={
            "scorer": dict(SCORER),
            "economy_mode": True,
            "generation_models": [{"name": "fixture/model", "extra": {"economy_mode": True}}],
        },
        execution_budget_id="draft",
        execution_budget_revision=1,
    )

    status, _ = _perform_preflight(
        request,
        SimpleNamespace(username="alice"),
        object(),
        SimpleNamespace(id="budget", generation=1),
        {"id": "setup"},
        attempt=1,
        engine=object(),
    )

    assert status == "succeeded"
    assert [option.get("allow_batch") for option in options] == [False]

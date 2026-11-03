"""Keep debug-preview model calls unbatched, even for an economy-marked model (SKY-285)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from core.api import protected_preview

from .test_protected_previews import _workflow_payload
from .test_protected_previews import previews as _previews_fixture
from .test_protected_submissions import harness as _harness_fixture

harness = _harness_fixture
previews = _previews_fixture


def test_preview_forbids_a_batched_model_route(previews, monkeypatch: pytest.MonkeyPatch) -> None:
    """Send ``allow_batch=False`` to the gateway when the preview model asks for economy."""
    harness, state = previews
    options: list[dict[str, Any]] = []
    economy_flags: list[Any] = []
    fixture_gateway = protected_preview.ModelGateway

    class _RecordingGateway(fixture_gateway):
        """Record the protection options the preview sends, then keep the fixture transport."""

        def protect_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            """Record the options and the model's economy flag before the fixture protection.

            Args:
                payload: The credential-resolved preview payload.
                **kwargs: The protection options under test.

            Returns:
                The fixture's protected payload.
            """
            options.append(kwargs)
            economy_flags.append(payload["model_config"]["extra"].get("economy_mode"))
            return super().protect_payload(payload, **kwargs)

    monkeypatch.setattr(protected_preview, "ModelGateway", _RecordingGateway)
    payload = _workflow_payload()
    payload["model_config"] = {"name": "fixture/model", "token_source": "managed", "extra": {"economy_mode": True}}
    monkeypatch.setattr(protected_preview.settings, "openrouter_api_key", SecretStr("fixture-only"))

    response = harness.client.post("/workflows/dry-run", json=payload)

    assert response.status_code == 200, response.text
    assert response.json()["preview_status"] == "succeeded"
    assert economy_flags == [True]
    assert [option.get("allow_batch") for option in options] == [False]
    assert len(state["calls"]) == 1

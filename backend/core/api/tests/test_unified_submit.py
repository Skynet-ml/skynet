"""Tests for ``POST /optimize`` request validation and the stored DSPy payload shape.

``/optimize`` refuses every new DSPy program run with a 410 (SKY-316); the
retirement itself is tested in ``test_submissions_router.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ...models import UnifiedRunRequest, optimizers_for
from .test_submissions_router import _FakeJobStore, make_client, unified_payload


def _submit(client: Any, body: dict) -> tuple[int, dict]:
    """Post one unified run.

    Args:
        client: Test client over the submissions router.
        body: Request body.

    Returns:
        The status code and the JSON body.
    """
    resp = client.post("/optimize", json=body)
    return resp.status_code, resp.json()


def test_optimizer_outside_the_setup_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """A black-box engine on a DSPy program is rejected by request validation."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    status, body = _submit(client, unified_payload(optimizer={"choice": "gepa"}))

    assert status == 422
    assert "gepa" in str(body) or "optimizer" in str(body).lower()
    assert store.created_ids() == []


@pytest.mark.parametrize("data_mode", ["pairs", "single"])
def test_a_new_run_cannot_choose_a_dspy_optimizer(data_mode: str) -> None:
    """The choices a new run may pick hold no DSPy optimizer; a repository run defaults to Auto (SKY-316).

    Args:
        data_mode: Single input or pairs.
    """
    assert optimizers_for("dspy_program", data_mode) == []
    repo = optimizers_for("repo", data_mode)
    assert repo[0] == "auto"
    assert not any(choice.startswith("dspy_") for choice in repo)


@pytest.mark.parametrize("choice", ["dspy_gepa", "dspy_miprov2", "dspy_bootstrap_fewshot", "auto"])
def test_a_stored_dspy_payload_still_parses(choice: str) -> None:
    """The worker re-parses stored payloads, so the old DSPy choices stay valid there.

    Args:
        choice: The stored optimizer choice.
    """
    request = UnifiedRunRequest.model_validate(unified_payload(optimizer={"choice": choice}))
    assert request.optimizer.choice == choice

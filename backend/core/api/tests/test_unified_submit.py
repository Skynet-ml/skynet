"""Tests for ``POST /optimize`` request validation and the optimizer choices a new run may pick.

``/optimize`` refuses every new DSPy program run with a 410 (SKY-316); the
retirement itself is tested in ``test_submissions_router.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ...models import optimizers_for
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


def test_a_retired_dspy_optimizer_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DSPy optimizer id is no longer a valid choice, so request validation rejects it."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    status, body = _submit(client, unified_payload(optimizer={"choice": "dspy_gepa"}))

    assert status == 422
    assert "optimizer" in str(body).lower()
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

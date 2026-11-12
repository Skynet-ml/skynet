"""Tests for ``POST /optimize`` with a DSPy optimizer and the optimizer choices a new run may pick.

``/optimize`` refuses every body with a 410 (SKY-316); the empty body is
tested in ``test_submissions_router.py``.
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


def test_a_retired_dspy_optimizer_is_410(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body with a DSPy optimizer id gets the same 410: the route validates no body."""
    store = _FakeJobStore()
    client = make_client(store, monkeypatch=monkeypatch)

    status, body = _submit(client, unified_payload(optimizer={"choice": "dspy_gepa"}))

    assert status == 410
    assert body["code"] == "optimization.dspy_run_retired"
    assert store.created_ids() == []


@pytest.mark.parametrize("data_mode", ["pairs", "single"])
def test_a_new_run_cannot_choose_a_dspy_optimizer(data_mode: str) -> None:
    """The choices a new run may pick hold no DSPy optimizer; a repository run defaults to Auto (SKY-316).

    Args:
        data_mode: Single input or pairs.
    """
    repo = optimizers_for(data_mode)
    assert repo[0] == "auto"
    assert not any(choice.startswith("dspy_") for choice in repo)

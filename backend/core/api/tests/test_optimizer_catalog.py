"""Tests for ``GET /optimizers``: which optimizers fit a setup and whether they can run here."""

from __future__ import annotations

import pytest

from ...service_gateway.optimization.blackbox import service as _bb_service
from .test_submissions_router import _FakeJobStore, make_client


@pytest.fixture
def runtimes_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report every sandbox and proposer runtime as available.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(_bb_service, "agent_target_unavailable_reason", lambda _settings: None)
    monkeypatch.setattr(_bb_service, "native_runtime_unavailable_reason", lambda _runtime, _settings: None)


def _catalog(monkeypatch: pytest.MonkeyPatch, artifact_kind: str, data_mode: str) -> dict:
    """Fetch the catalog for one setup.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        artifact_kind: What the run optimizes.
        data_mode: Single input or pairs.

    Returns:
        The response JSON.
    """
    client = make_client(_FakeJobStore(), monkeypatch=monkeypatch)
    resp = client.get("/optimizers", params={"artifact_kind": artifact_kind, "data_mode": data_mode})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.usefixtures("runtimes_up")
@pytest.mark.parametrize("data_mode", ["pairs", "single"])
def test_dspy_program_offers_no_optimizer_to_a_new_run(monkeypatch: pytest.MonkeyPatch, data_mode: str) -> None:
    """A new run cannot pick a DSPy optimizer, so a DSPy program gets no options and no Auto.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        data_mode: Single input or pairs.
    """
    body = _catalog(monkeypatch, "dspy_program", data_mode)

    assert body["options"] == []
    assert body["auto_resolves_to"] == []
    assert body["run_entry"] == "def run(artifact, input)"


@pytest.mark.usefixtures("runtimes_up")
def test_repo_offers_auto_first_then_the_blackbox_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository run gets Auto as the default, then every black-box engine, under the same contract."""
    body = _catalog(monkeypatch, "repo", "pairs")

    by_id = {option["id"]: option for option in body["options"]}
    assert list(by_id) == ["auto", "gepa", "best_of_n", "autoresearch", "meta_harness", "autosaddler", "shinka_evolve"]
    assert body["default"] == "auto"
    assert body["auto_resolves_to"] == ["auto"]
    assert by_id["auto"]["family"] == "auto"
    assert by_id["auto"]["uses_feedback"] is True
    assert body["run_entry"] == "def run(artifact, input)"
    assert body["metric_signature"] == "def score(input, output, expected=None)"
    assert by_id["gepa"]["family"] == "blackbox"
    assert by_id["gepa"]["uses_feedback"] is True
    assert by_id["best_of_n"]["uses_feedback"] is False
    assert by_id["autoresearch"]["native_proposer"] is True
    assert by_id["gepa"]["native_proposer"] is False
    assert body["sandbox_available"] is True
    assert all(option["available"] for option in body["options"])
    assert all(option["family"] != "dspy" for option in body["options"])


def test_blackbox_availability_follows_the_engine_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engines that need the managed sandbox are unavailable with its reason, and so is Auto."""
    monkeypatch.setattr(_bb_service, "agent_target_unavailable_reason", lambda _settings: None)
    monkeypatch.setattr(
        _bb_service,
        "native_runtime_unavailable_reason",
        lambda runtime, _settings: "Managed sandbox is missing" if runtime == "vercel" else None,
    )

    repo = _catalog(monkeypatch, "repo", "pairs")

    by_id = {option["id"]: option for option in repo["options"]}
    assert by_id["gepa"]["available"] is True
    assert by_id["gepa"]["unavailable_reason"] is None
    assert by_id["autoresearch"]["available"] is False
    assert "Managed sandbox is missing" in by_id["autoresearch"]["unavailable_reason"]
    assert by_id["auto"]["available"] is False
    assert by_id["auto"]["unavailable_reason"]


def test_missing_sandbox_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The evaluator sandbox reason is passed through for the wizard to show."""
    monkeypatch.setattr(_bb_service, "agent_target_unavailable_reason", lambda _settings: "no sandbox here")
    monkeypatch.setattr(_bb_service, "native_runtime_unavailable_reason", lambda _runtime, _settings: None)

    body = _catalog(monkeypatch, "dspy_program", "single")

    assert body["sandbox_available"] is False
    assert body["sandbox_reason"] == "no sandbox here"


@pytest.mark.parametrize(
    "params",
    [
        {"artifact_kind": "prompt", "data_mode": "pairs"},
        {"artifact_kind": "dspy_program", "data_mode": "many"},
        {"artifact_kind": "dspy_program"},
    ],
)
def test_unknown_setup_is_422(monkeypatch: pytest.MonkeyPatch, params: dict) -> None:
    """Artifact kinds and data modes outside the contract are rejected.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        params: Query parameters to send.
    """
    client = make_client(_FakeJobStore(), monkeypatch=monkeypatch)

    assert client.get("/optimizers", params=params).status_code == 422

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
def test_dspy_program_with_pairs_offers_every_dspy_optimizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DSPy program with pairs gets Auto and the three DSPy optimizers, all available."""
    body = _catalog(monkeypatch, "dspy_program", "pairs")

    by_id = {option["id"]: option for option in body["options"]}
    assert list(by_id) == ["auto", "dspy_gepa", "dspy_miprov2", "dspy_bootstrap_fewshot"]
    assert body["default"] == "auto"
    assert body["auto_resolves_to"] == ["dspy_gepa"]
    assert all(option["available"] for option in body["options"])
    assert by_id["dspy_gepa"]["family"] == "dspy"
    assert by_id["dspy_gepa"]["uses_feedback"] is True
    assert by_id["dspy_miprov2"]["uses_feedback"] is False
    assert by_id["dspy_bootstrap_fewshot"]["needs_reflection_model"] is False
    assert by_id["auto"]["family"] == "auto"
    assert body["run_entry"] == "def run(artifact, input)"
    assert body["metric_signature"] == "def score(input, output, expected=None)"
    assert all("metric_shape" not in option for option in body["options"])


@pytest.mark.usefixtures("runtimes_up")
def test_dspy_program_with_one_input_offers_only_gepa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without pairs, only the feedback-driven DSPy optimizer fits."""
    body = _catalog(monkeypatch, "dspy_program", "single")

    assert [option["id"] for option in body["options"]] == ["auto", "dspy_gepa"]


@pytest.mark.usefixtures("runtimes_up")
def test_repo_offers_the_blackbox_engines_without_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository run gets every black-box engine and no Auto, under the same contract."""
    body = _catalog(monkeypatch, "repo", "pairs")

    by_id = {option["id"]: option for option in body["options"]}
    assert list(by_id) == ["gepa", "best_of_n", "autoresearch", "meta_harness", "autosaddler", "shinka_evolve"]
    assert body["run_entry"] == "def run(artifact, input)"
    assert body["metric_signature"] == "def score(input, output, expected=None)"
    assert by_id["gepa"]["family"] == "blackbox"
    assert by_id["gepa"]["uses_feedback"] is True
    assert by_id["best_of_n"]["uses_feedback"] is False
    assert by_id["autoresearch"]["native_proposer"] is True
    assert by_id["gepa"]["native_proposer"] is False
    assert body["auto_resolves_to"] == []
    assert body["sandbox_available"] is True
    assert all(option["available"] for option in body["options"])


def test_blackbox_availability_follows_the_engine_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engines that need the managed sandbox are unavailable with its reason; DSPy stays available."""
    monkeypatch.setattr(_bb_service, "agent_target_unavailable_reason", lambda _settings: None)
    monkeypatch.setattr(
        _bb_service,
        "native_runtime_unavailable_reason",
        lambda runtime, _settings: "Managed sandbox is missing" if runtime == "vercel" else None,
    )

    repo = _catalog(monkeypatch, "repo", "pairs")
    dspy = _catalog(monkeypatch, "dspy_program", "pairs")

    by_id = {option["id"]: option for option in repo["options"]}
    assert by_id["gepa"]["available"] is True
    assert by_id["gepa"]["unavailable_reason"] is None
    assert by_id["autoresearch"]["available"] is False
    assert "Managed sandbox is missing" in by_id["autoresearch"]["unavailable_reason"]
    assert all(option["available"] for option in dspy["options"])


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

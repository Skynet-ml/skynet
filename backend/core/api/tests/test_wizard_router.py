"""Tests for the ``/wizard/update`` field-validation surface."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ..routers.wizard import create_wizard_router


@pytest.fixture
def wizard_client() -> TestClient:
    """Build a ``TestClient`` exposing only the wizard router.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_wizard_router())
    return TestClient(app, raise_server_exceptions=False)


def test_is_private_accepted_and_echoed(wizard_client: TestClient) -> None:
    """The privacy toggle round-trips so the agent can make a run public."""
    resp = wizard_client.post("/wizard/update", json={"is_private": False})

    assert resp.status_code == 200
    assert resp.json()["wizard_state"]["is_private"] is False


def test_model_config_missing_prefix_rejected(wizard_client: TestClient) -> None:
    """A bare model name with no provider prefix is rejected by the prefix guard."""
    resp = wizard_client.post("/wizard/update", json={"model_config": {"name": "gpt-4o-mini"}})

    assert resp.status_code == 422
    assert "gpt-4o-mini" in resp.json()["detail"]
    assert "prefix" in resp.json()["detail"].lower()


def test_model_config_prefixed_accepted(wizard_client: TestClient) -> None:
    """A provider-prefixed model name is accepted and echoed back trimmed."""
    resp = wizard_client.post("/wizard/update", json={"model_config": {"name": "  openai/gpt-4o-mini  "}})

    assert resp.status_code == 200
    patch = resp.json()["wizard_state"]
    assert patch["model_config"]["name"] == "openai/gpt-4o-mini"
    assert patch["model_configured"] is True


@pytest.mark.parametrize("field", ["run_code", "metric_code"])
def test_workspace_code_round_trips_unchanged(wizard_client: TestClient, field: str) -> None:
    """The wizard's agent writes workspace code; it comes back with its whitespace kept."""
    code = "def run(artifact, input):\n    return input\n"
    resp = wizard_client.post("/wizard/update", json={field: code})

    assert resp.status_code == 200
    assert resp.json()["wizard_state"] == {field: code}


@pytest.mark.parametrize("field", ["run_code", "metric_code"])
def test_blank_workspace_code_rejected(wizard_client: TestClient, field: str) -> None:
    """Blank code would wipe the file, so the endpoint refuses it."""
    resp = wizard_client.post("/wizard/update", json={field: "   "})

    assert resp.status_code == 422
    assert field in resp.json()["detail"]


def test_task_fields_round_trip(wizard_client: TestClient) -> None:
    """Objective, background, artifact kind and starting version land under their v2 keys."""
    body = {
        "task_objective": "  Make it shorter  ",
        "task_background": "Support replies.",
        "artifact_kind": "dspy_program",
        "starting_version": {"predict": "You are a helpful bot."},
    }
    resp = wizard_client.post("/wizard/update", json=body)

    assert resp.status_code == 200
    patch = resp.json()["wizard_state"]
    assert patch["task_objective"] == "Make it shorter"
    assert patch["task_background"] == "Support replies."
    assert patch["artifact_kind"] == "dspy_program"
    assert patch["starting_version"] == {"predict": "You are a helpful bot."}


def test_repo_target_round_trips_without_a_starting_version(wizard_client: TestClient) -> None:
    """A repository run takes the repo kind and an owner/name, with no starting version."""
    resp = wizard_client.post("/wizard/update", json={"artifact_kind": "repo", "repository": "acme/support-bot"})

    assert resp.status_code == 200
    patch = resp.json()["wizard_state"]
    assert patch == {"artifact_kind": "repo", "repository": "acme/support-bot"}


@pytest.mark.parametrize("name", ["acme", "acme/bot/extra", "https://github.com/acme/bot", "acme/ bot"])
def test_malformed_repository_rejected(wizard_client: TestClient, name: str) -> None:
    """Only a bare owner/name reaches the repository picker."""
    resp = wizard_client.post("/wizard/update", json={"repository": name})

    assert resp.status_code == 422


@pytest.mark.parametrize("kind", ["prompt", "code", "text"])
def test_other_artifact_kinds_are_refused_for_new_runs(wizard_client: TestClient, kind: str) -> None:
    """New runs take a DSPy program or a repository, nothing else."""
    resp = wizard_client.post("/wizard/update", json={"artifact_kind": kind})

    assert resp.status_code == 422


def test_multi_part_starting_version_round_trips(wizard_client: TestClient) -> None:
    """A named-parts starting version is echoed as a dict."""
    parts = {"system": "Be brief.", "user": "Answer: {q}"}
    resp = wizard_client.post("/wizard/update", json={"starting_version": parts})

    assert resp.status_code == 200
    assert resp.json()["wizard_state"]["starting_version"] == parts


def test_blank_objective_rejected(wizard_client: TestClient) -> None:
    """A blank objective is refused like every other non-empty string field."""
    resp = wizard_client.post("/wizard/update", json={"task_objective": "   "})

    assert resp.status_code == 422


def test_inputs_fields_round_trip(wizard_client: TestClient) -> None:
    """Data mode, staged dataset and split land in the patch."""
    resp = wizard_client.post(
        "/wizard/update",
        json={
            "data_mode": "pairs",
            "staged_dataset_id": "stage-1",
            "split_fractions": {"train": 0.6, "val": 0.2, "test": 0.2},
            "shuffle": False,
        },
    )

    assert resp.status_code == 200
    patch = resp.json()["wizard_state"]
    assert patch["data_mode"] == "pairs"
    assert patch["staged_dataset_id"] == "stage-1"
    assert patch["split_fractions"] == {"train": 0.6, "val": 0.2, "test": 0.2}
    assert patch["shuffle"] is False


def test_optimizer_choice_fitting_setup_accepted(wizard_client: TestClient) -> None:
    """A DSPy optimizer fits a DSPy program with pairs."""
    resp = wizard_client.post(
        "/wizard/update",
        json={"artifact_kind": "dspy_program", "data_mode": "pairs", "optimizer_choice": "dspy_miprov2"},
    )

    assert resp.status_code == 200
    assert resp.json()["wizard_state"]["optimizer_choice"] == "dspy_miprov2"


def test_optimizer_choice_outside_setup_rejected(wizard_client: TestClient) -> None:
    """A black-box engine does not fit a DSPy program."""
    resp = wizard_client.post(
        "/wizard/update",
        json={"artifact_kind": "dspy_program", "data_mode": "single", "optimizer_choice": "best_of_n"},
    )

    assert resp.status_code == 422


def test_optimizer_choice_alone_is_echoed(wizard_client: TestClient) -> None:
    """Without the artifact and mode in the same patch the choice is echoed as-is."""
    resp = wizard_client.post("/wizard/update", json={"optimizer_choice": "gepa"})

    assert resp.status_code == 200
    assert resp.json()["wizard_state"]["optimizer_choice"] == "gepa"


@pytest.mark.parametrize(
    "body",
    [
        {"data_mode": "many"},
        {"optimizer_choice": "grid_search"},
        {"spending_limit_usd": 0},
        {"spending_limit_usd": -5},
        {"split_fractions": {"train": 0.5, "val": 0.1, "test": 0.1}},
    ],
)
def test_invalid_values_rejected(wizard_client: TestClient, body: dict) -> None:
    """Unknown kinds, modes, optimizers, non-positive limits and bad splits are 422."""
    resp = wizard_client.post("/wizard/update", json=body)

    assert resp.status_code == 422


@pytest.mark.parametrize(
    "legacy_key",
    ["job_type", "module_name", "column_roles", "blackbox_objective", "signature_code", "generation_models"],
)
def test_retired_keys_are_not_echoed(wizard_client: TestClient, legacy_key: str) -> None:
    """Keys of the retired wizards no longer reach the patch."""
    resp = wizard_client.post("/wizard/update", json={legacy_key: "x"})

    assert resp.status_code == 200
    assert legacy_key not in resp.json()["wizard_state"]


def test_spending_limit_converts_to_cents(wizard_client: TestClient) -> None:
    """A dollar limit lands as ``max_cost_cents``, the submit payload's field."""
    resp = wizard_client.post("/wizard/update", json={"spending_limit_usd": 12.5})

    assert resp.status_code == 200
    state = resp.json()["wizard_state"]
    assert state["max_cost_cents"] == 1250
    assert "spending_limit_usd" not in state


def test_spending_limit_null_removes_the_limit(wizard_client: TestClient) -> None:
    """An explicit null clears the limit; omitting the field leaves it alone."""
    cleared = wizard_client.post("/wizard/update", json={"spending_limit_usd": None})
    untouched = wizard_client.post("/wizard/update", json={"is_private": True})

    assert cleared.status_code == 200
    assert "max_cost_cents" in cleared.json()["wizard_state"]
    assert cleared.json()["wizard_state"]["max_cost_cents"] is None
    assert "max_cost_cents" not in untouched.json()["wizard_state"]

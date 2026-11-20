"""Tests for the ``/wizard/update`` field-validation surface."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ...config import settings
from .. import model_catalog
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
    """Objective and background land under their v2 keys."""
    body = {
        "task_objective": "  Make it shorter  ",
        "task_background": "Support replies.",
    }
    resp = wizard_client.post("/wizard/update", json=body)

    assert resp.status_code == 200
    patch = resp.json()["wizard_state"]
    assert patch["task_objective"] == "Make it shorter"
    assert patch["task_background"] == "Support replies."


def test_repo_target_round_trips_without_a_starting_version(wizard_client: TestClient) -> None:
    """A repository run takes an owner/name, with no starting version."""
    resp = wizard_client.post("/wizard/update", json={"repository": "acme/support-bot"})

    assert resp.status_code == 200
    patch = resp.json()["wizard_state"]
    assert patch == {"repository": "acme/support-bot"}


@pytest.mark.parametrize("name", ["acme", "acme/bot/extra", "https://github.com/acme/bot", "acme/ bot"])
def test_malformed_repository_rejected(wizard_client: TestClient, name: str) -> None:
    """Only a bare owner/name reaches the repository picker."""
    resp = wizard_client.post("/wizard/update", json={"repository": name})

    assert resp.status_code == 422


@pytest.mark.parametrize("kind", ["repo", "dspy_program"])
def test_a_stale_artifact_kind_is_ignored(wizard_client: TestClient, kind: str) -> None:
    """An old tab or draft may still send ``artifact_kind``; the update drops it and keeps the rest.

    Args:
        wizard_client: The wizard router client.
        kind: An artifact kind an old client sent.
    """
    resp = wizard_client.post("/wizard/update", json={"artifact_kind": kind, "repository": "acme/support-bot"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["wizard_state"] == {"repository": "acme/support-bot"}


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


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_job_description_clears_it(wizard_client: TestClient, value: str) -> None:
    """An empty description is optional, so it clears the field instead of a 422."""
    resp = wizard_client.post("/wizard/update", json={"job_name": "My run", "job_description": value})

    assert resp.status_code == 200, resp.text
    assert resp.json()["wizard_state"] == {"job_name": "My run", "job_description": ""}


def test_blank_job_name_still_rejected(wizard_client: TestClient) -> None:
    """The run name stays required."""
    resp = wizard_client.post("/wizard/update", json={"job_name": "  "})

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


@pytest.mark.parametrize("choice", ["auto", "gepa", "shinka_evolve"])
def test_optimizer_choice_fitting_setup_accepted(wizard_client: TestClient, choice: str) -> None:
    """Auto and the repository engines fit a repository run with pairs."""
    resp = wizard_client.post(
        "/wizard/update",
        json={"data_mode": "pairs", "optimizer_choice": choice},
    )

    assert resp.status_code == 200
    assert resp.json()["wizard_state"]["optimizer_choice"] == choice


@pytest.mark.parametrize(
    "body",
    [
        {"data_mode": "pairs", "optimizer_choice": "grid_search"},
        {"optimizer_choice": "unknown_engine"},
        {"optimizer_choice": "dspy_gepa"},
    ],
)
def test_optimizer_choice_outside_setup_rejected(wizard_client: TestClient, body: dict) -> None:
    """Skynet is repo-only, so unknown and DSPy optimizers are refused."""
    resp = wizard_client.post("/wizard/update", json=body)

    assert resp.status_code == 422


def test_optimizer_choice_alone_is_echoed(wizard_client: TestClient) -> None:
    """A repository engine sent alone is checked against the repo list and echoed."""
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


_VALID_TOML = """
version = 1

[paths]
editable = ["src"]

[run]
entry = "src/run.py:main"
"""


def test_a_valid_ascent_toml_checks_ok(wizard_client: TestClient) -> None:
    """A file the parser accepts comes back ok with no errors."""
    resp = wizard_client.post("/wizard/ascent-toml/check", json={"text": _VALID_TOML})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "errors": [], "metric_command": None, "proposer_command": None}


@pytest.mark.parametrize(
    ("metric", "command"),
    [
        (
            '[metric]\nfiles = ["eval"]\nentry = ["cargo", "run", "--manifest-path", "eval/Cargo.toml"]\n',
            ["cargo", "run", "--manifest-path", "eval/Cargo.toml"],
        ),
        ('[metric]\nentry = "eval/score.py:grade"\n', None),
        ("", None),
    ],
)
def test_the_check_names_the_metric_command_only_for_a_command_entry(
    wizard_client: TestClient, metric: str, command: list[str] | None
) -> None:
    """A command ``[metric] entry`` comes back as its argv; a Python entry or none gives null (SKY-770)."""
    resp = wizard_client.post("/wizard/ascent-toml/check", json={"text": _VALID_TOML + metric})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "errors": [], "metric_command": command, "proposer_command": None}


def test_a_bad_file_names_no_metric_command(wizard_client: TestClient) -> None:
    """A file the parser refuses names no command, so the wizard still asks for metric.py (SKY-770)."""
    text = _VALID_TOML + '[metric]\nentry = ["bash", "metric.sh"]\n'
    resp = wizard_client.post("/wizard/ascent-toml/check", json={"text": text})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["metric_command"] is None


def test_the_check_names_the_proposer_command_of_the_file(wizard_client: TestClient) -> None:
    """A ``[proposer] entry`` comes back as its argv, so the picker offers it first (SKY-771)."""
    text = _VALID_TOML + '[proposer]\nentry = ["python", "agent/propose.py"]\n'
    resp = wizard_client.post("/wizard/ascent-toml/check", json={"text": text})

    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "errors": [],
        "metric_command": None,
        "proposer_command": ["python", "agent/propose.py"],
    }


def test_a_bad_file_names_no_proposer_command(wizard_client: TestClient) -> None:
    """A file the parser refuses names no proposer, so the picker stays as it is (SKY-771)."""
    text = 'version = 1\n[proposer]\nentry = ["python", "agent/propose.py"]\n'
    resp = wizard_client.post("/wizard/ascent-toml/check", json={"text": text})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["proposer_command"] is None


@pytest.mark.parametrize(
    ("text", "section", "key", "message"),
    [
        (
            'version = 1\n[target.planner]\nentry = "a.py:f"\nvcpus = 3\n',
            "target.planner",
            "vcpus",
            "vcpus must be 1 or an even",
        ),
        ("version = 1\n[paths]\neditable = []\n", "paths", "editable", "editable is empty"),
        ("version = 2\n", None, "version", "version 2 is not supported"),
        ('version = 1\n[paths]\neditable = ["src"]\n', None, None, "Add a [run] section or a [target.<name>] stage"),
        ("[paths\n", None, None, "is not valid TOML"),
    ],
)
def test_a_bad_ascent_toml_returns_the_error_and_its_place(
    wizard_client: TestClient, text: str, section: str | None, key: str | None, message: str
) -> None:
    """A parser error comes back first, with the section and key the editor shows it next to."""
    resp = wizard_client.post("/wizard/ascent-toml/check", json={"text": text})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    error = body["errors"][0]
    assert (error["level"], error["section"], error["key"]) == ("error", section, key)
    assert message in error["message"]


# Three mistakes in three sections, on lines 4, 6 and 8.
_THREE_MISTAKES = """version = 1
[run]
entry = "a.py:f"
bogus = 1
[metric]
entry = 5
[paths]
editable = 3
"""


def test_a_file_with_three_mistakes_returns_each_with_its_level_and_line(wizard_client: TestClient) -> None:
    """One check returns every mistake, each with its level and line, in the parser's order (SKY-763)."""
    resp = wizard_client.post("/wizard/ascent-toml/check", json={"text": _THREE_MISTAKES})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert [(e["level"], e["line"], e["section"], e["key"]) for e in body["errors"]] == [
        ("error", 4, "run", "bogus"),
        ("error", 8, "paths", "editable"),
        ("error", 6, "metric", "entry"),
    ]
    assert body["errors"][0]["message"] == "ascent.toml: [run] bogus is not a supported setting."


def test_a_lone_surrogate_in_the_text_is_a_parser_error_not_a_crash(wizard_client: TestClient) -> None:
    """JSON can carry a lone surrogate that UTF-8 cannot encode; the parser refuses it as bad text."""
    body = '{"text": "instructions = \\"\\ud800\\"\\n"}'
    resp = wizard_client.post("/wizard/ascent-toml/check", content=body, headers={"content-type": "application/json"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "is not valid TOML" in resp.json()["errors"][0]["message"]


def _catalog_with_efforts(value: str, efforts: list[str] | None) -> SimpleNamespace:
    """Build a stand-in model catalog with one model and its effort levels.

    Args:
        value: The model's catalog id.
        efforts: The model's ``reasoning_efforts`` list.

    Returns:
        An object shaped like ``ModelCatalogResponse`` for ``get_catalog_cached``.
    """
    return SimpleNamespace(models=[SimpleNamespace(value=value, reasoning_efforts=efforts)])


_OPUS = "openrouter/anthropic/claude-opus-5.5"


@pytest.fixture
def opus_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve a catalog where Opus accepts low, medium and high.

    Args:
        monkeypatch: Pytest fixture that replaces the catalog reader.
    """
    monkeypatch.setattr(
        model_catalog, "get_catalog_cached", lambda: _catalog_with_efforts(_OPUS, ["low", "medium", "high"])
    )


@pytest.mark.parametrize("field", ["model_config", "reflection_model_config"])
def test_unknown_model_config_key_steers_to_extra(wizard_client: TestClient, field: str) -> None:
    """A top-level reasoning_effort is refused with an error that names the right place (SKY-747)."""
    resp = wizard_client.post("/wizard/update", json={field: {"name": _OPUS, "reasoning_effort": "medium"}})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "reasoning_effort" in detail
    assert "name, token_source, byok_provider, cli_provider, base_url, temperature, max_tokens, extra" in detail
    assert f"{field}.extra.reasoning_effort" in detail


@pytest.mark.parametrize("field", ["model_config", "reflection_model_config"])
def test_reasoning_effort_in_extra_is_saved(wizard_client: TestClient, opus_catalog: None, field: str) -> None:
    """A supported level in extra.reasoning_effort is echoed back unchanged."""
    resp = wizard_client.post("/wizard/update", json={field: {"name": _OPUS, "extra": {"reasoning_effort": "medium"}}})

    assert resp.status_code == 200
    assert resp.json()["wizard_state"][field]["extra"] == {"reasoning_effort": "medium"}


@pytest.mark.parametrize("field", ["model_config", "reflection_model_config"])
def test_unsupported_reasoning_effort_lists_the_levels(
    wizard_client: TestClient, opus_catalog: None, field: str
) -> None:
    """A level the catalog does not list for the model is refused with the supported levels."""
    resp = wizard_client.post("/wizard/update", json={field: {"name": _OPUS, "extra": {"reasoning_effort": "max"}}})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "'max'" in detail
    assert "low, medium, high" in detail


@pytest.mark.parametrize(
    "catalog",
    [
        _catalog_with_efforts("openai/other-model", ["low"]),
        _catalog_with_efforts(_OPUS, None),
        _catalog_with_efforts(_OPUS, []),
    ],
    ids=["no-entry", "levels-unknown", "no-levels"],
)
def test_extra_kept_as_today_without_catalog_levels(
    wizard_client: TestClient, monkeypatch: pytest.MonkeyPatch, catalog: SimpleNamespace
) -> None:
    """A model with no catalog entry or no levels keeps any extra.reasoning_effort."""
    monkeypatch.setattr(model_catalog, "get_catalog_cached", lambda: catalog)
    extra = {"reasoning_effort": "max", "top_p": 0.5}

    resp = wizard_client.post("/wizard/update", json={"model_config": {"name": _OPUS, "extra": extra}})

    assert resp.status_code == 200
    assert resp.json()["wizard_state"]["model_config"]["extra"] == extra


def test_extra_kept_when_the_catalog_fails(wizard_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unavailable catalog does not block the save."""

    def _fail() -> None:
        """Raise like a catalog that cannot load."""
        raise RuntimeError("catalog down")

    monkeypatch.setattr(model_catalog, "get_catalog_cached", _fail)

    resp = wizard_client.post(
        "/wizard/update", json={"model_config": {"name": _OPUS, "extra": {"reasoning_effort": "max"}}}
    )

    assert resp.status_code == 200


def test_ui_model_config_keys_are_kept(wizard_client: TestClient) -> None:
    """The real ModelConfig keys that the UI sends are echoed, not refused or dropped."""
    config = {
        "name": "openai/gpt-4o-mini",
        "token_source": "byok",
        "byok_provider": "openai",
        "cli_provider": None,
        "base_url": None,
        "temperature": 0.2,
        "max_tokens": 100,
        "extra": {},
    }

    resp = wizard_client.post("/wizard/update", json={"model_config": config})

    assert resp.status_code == 200
    echoed = resp.json()["wizard_state"]["model_config"]
    assert echoed["token_source"] == "byok"
    assert echoed["byok_provider"] == "openai"


@pytest.mark.parametrize("key", ["token_source", "byok_provider", "cli_provider"])
def test_non_string_ui_model_config_key_rejected(wizard_client: TestClient, key: str) -> None:
    """A UI key with a non-string value fails with 422."""
    resp = wizard_client.post("/wizard/update", json={"model_config": {"name": _OPUS, key: 5}})

    assert resp.status_code == 422
    assert key in resp.json()["detail"]


@pytest.mark.parametrize(("key", "value"), [("token_source", "paid"), ("cli_provider", "gemini")])
def test_ui_model_config_key_outside_its_values_rejected(wizard_client: TestClient, key: str, value: str) -> None:
    """A token_source or cli_provider that ModelConfig does not allow fails with the allowed values."""
    resp = wizard_client.post("/wizard/update", json={"model_config": {"name": _OPUS, key: value}})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert f"'{value}'" in detail
    assert ("managed, byok, subscription" if key == "token_source" else "codex, claude") in detail


def test_subscription_without_cli_provider_rejected(wizard_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A subscription model with no cli_provider fails now, not later at submit."""
    monkeypatch.setattr(settings, "local_mode", True)

    resp = wizard_client.post("/wizard/update", json={"model_config": {"name": _OPUS, "token_source": "subscription"}})

    assert resp.status_code == 422
    assert "cli_provider" in resp.json()["detail"]


def test_subscription_with_cli_provider_kept_in_local_mode(
    wizard_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local build keeps a subscription model that names its CLI."""
    monkeypatch.setattr(settings, "local_mode", True)
    config = {"name": _OPUS, "token_source": "subscription", "cli_provider": "codex"}

    resp = wizard_client.post("/wizard/update", json={"model_config": config})

    assert resp.status_code == 200
    assert resp.json()["wizard_state"]["model_config"]["cli_provider"] == "codex"


def test_subscription_rejected_on_hosted_build(wizard_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hosted build refuses a subscription model, as submit does."""
    monkeypatch.setattr(settings, "local_mode", False)
    config = {"name": _OPUS, "token_source": "subscription", "cli_provider": "codex"}

    resp = wizard_client.post("/wizard/update", json={"model_config": config})

    assert resp.status_code == 422
    assert "subscription" in resp.json()["detail"]

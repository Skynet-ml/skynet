"""Tests for the generalist-agent tool gating and approval registry."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import dspy
import litellm
import pytest
from fastapi.routing import APIRoute
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from core.api.routers.folders import create_folders_router
from core.config import settings
from core.service_gateway.agents import docs_tools, web_tools
from core.service_gateway.agents import generalist as generalist_module
from core.service_gateway.agents.code import AgentStoppedError, _agent_error_payload, _SubmitArgExtractor
from core.service_gateway.agents.docs_retrieval import DocsCorpus
from core.service_gateway.agents.generalist import (
    AUTOMATIC_TURN_PREFIX,
    CLONE_FOCUS,
    GENERALIST_SYSTEM_PROMPT,
    SCREEN_FOCUS,
    ApprovalRegistry,
    ThinkingSummaries,
    ThinkingTrail,
    WizardState,
    _clean_summary,
    _history_from_chat,
    _needs_approval,
    _screen_needs_approval,
    _screen_problems,
    _TurnAuthoringFlag,
    _wrap_tool_with_approval,
    approval_key,
    fallback_summary,
    note_sample_language,
    plan_tool,
    scope_wizard_tool,
    screen_nav_tools,
    screen_signature,
    screen_snapshot,
    screen_tools,
    screen_turn_guard,
    tools_for,
    validate_wizard_patch_order,
    validate_wizard_screen,
)
from core.storage.models import Base

_SUBMIT = "submit_unified_run_optimize_post"
_READY: dict[str, Any] = {
    "job_name": "My run",
    "task_objective": "Answer support questions",
    "artifact_kind": "dspy_program",
    "starting_version": "You are a helpful agent.",
    "data_mode": "pairs",
    "dataset_ready": True,
    "staged_dataset_id": "ds_1",
    "run_code": "def run(artifact, input):\n    return input['q']\n",
    "metric_code": "def score(input, output, expected=None):\n    return 1.0, {'feedback': ''}\n",
    "reflection_model_config": {"name": "openai/gpt-4o-mini"},
}


def test_empty_state_hides_dataset_and_submit_tools() -> None:
    """An empty wizard state hides dataset/code/submit tools, exposing only discovery."""
    allowed = tools_for(WizardState())
    assert "validate_code_validate_code_post" not in allowed
    assert _SUBMIT not in allowed
    assert "list_models_for_agent" in allowed


def test_removed_submit_tools_are_never_exposed() -> None:
    """The deleted /run, /grid-search and /blackbox/run tools never reappear."""
    allowed = tools_for(cast(WizardState, _READY))
    for removed in (
        "submit_job_run_post",
        "submit_grid_search_grid_search_post",
        "submit_blackbox_run_blackbox_run_post",
        "blackbox_scorer_dry_run_blackbox_scorer_dry_run_post",
    ):
        assert removed not in allowed


def test_comparison_tool_is_not_exposed() -> None:
    """Keep cross-optimization comparison out of every agent state."""
    assert "compare_jobs_optimizations_compare_post" not in tools_for(WizardState())


def test_dataset_ready_unlocks_diagnostics_but_not_submit() -> None:
    """A staged dataset exposes the diagnostics but not submit."""
    allowed = tools_for(WizardState(dataset_ready=True))
    assert "validate_code_validate_code_post" in allowed
    assert "profile_datasets_profile_post" in allowed
    assert _SUBMIT not in allowed


def test_full_readiness_unlocks_submit() -> None:
    """Every step settled exposes the one submit tool."""
    assert _SUBMIT in tools_for(cast(WizardState, _READY))


def test_single_mode_needs_no_staged_dataset() -> None:
    """A single-input run carries its case inline, so no dataset is needed to submit."""
    state = {**_READY, "data_mode": "single"}
    state.pop("dataset_ready")
    state.pop("staged_dataset_id")
    assert _SUBMIT in tools_for(cast(WizardState, state))


def test_missing_any_submit_precondition_keeps_submit_hidden() -> None:
    """Dropping any single precondition keeps submit hidden."""
    criteria: dict[str, dict[str, Any]] = {
        "job_name": {"job_name": ""},
        "task_objective": {"task_objective": ""},
        "artifact_kind": {"artifact_kind": None},
        "starting_version": {"starting_version": ""},
        "data_mode": {"data_mode": None},
        "dataset": {"dataset_ready": False, "staged_dataset_id": None},
        "run_code": {"run_code": ""},
        "metric_code": {"metric_code": ""},
        "reflection_model": {"reflection_model_config": {}},
    }
    for label, overrides in criteria.items():
        state = {**_READY, **overrides}
        assert _SUBMIT not in tools_for(cast(WizardState, state)), f"submit leaked with {label} missing"


def test_order_allows_name_first() -> None:
    """Setting ``job_name`` on an empty wizard is in order (Basics is first)."""
    assert validate_wizard_patch_order({"job_name": "My run"}, WizardState()) is None


def test_order_rejects_task_before_name() -> None:
    """Task fields require the run to be named first (Basics)."""
    err = validate_wizard_patch_order({"task_objective": "x"}, WizardState())
    assert err is not None
    assert "Basics" in err


def test_order_allows_name_and_task_in_one_patch() -> None:
    """A single patch may set the name AND the task — each satisfies its own step."""
    patch = {"job_name": "My run", "task_objective": "x", "artifact_kind": "dspy_program", "starting_version": "s"}
    assert validate_wizard_patch_order(patch, WizardState()) is None


def test_order_rejects_inputs_before_task() -> None:
    """The data mode can't be set before the task is settled, even with a name set."""
    err = validate_wizard_patch_order({"data_mode": "pairs"}, WizardState(job_name="My run"))
    assert err is not None
    assert "Task" in err


def test_order_rejects_optimizer_before_inputs() -> None:
    """The Optimizer step is gated on the Inputs step."""
    state = {k: _READY[k] for k in ("job_name", "task_objective", "artifact_kind", "starting_version")}
    err = validate_wizard_patch_order({"optimizer_choice": "gepa"}, cast(WizardState, state))
    assert err is not None
    assert "Inputs" in err


def test_order_allows_optimizer_after_inputs() -> None:
    """With name, task and inputs settled, the optimizer and models are in order."""
    patch = {"optimizer_choice": "gepa", "reflection_model_config": {"name": "openai/gpt-4o"}}
    assert validate_wizard_patch_order(patch, cast(WizardState, _READY)) is None


def test_screen_allows_any_field_when_wizard_not_on_view() -> None:
    """Without ``wizard_screen`` the wizard is off screen and nothing is scoped."""
    assert validate_wizard_screen("update_wizard_state", {"optimizer_choice": "auto"}, WizardState()) is None


def test_screen_allows_visible_and_screenless_fields() -> None:
    """The brief screen takes its own fields plus the run's name and description."""
    patch = {"job_name": "My run", "job_description": "d", "task_objective": "x", "task_background": "b"}
    assert validate_wizard_screen("update_wizard_state", patch, WizardState(wizard_screen="brief")) is None


def test_screen_rejects_fields_off_the_visible_screen() -> None:
    """A field from another screen is refused and named in the error."""
    patch = {"task_objective": "x", "artifact_kind": "dspy_program", "data_mode": "pairs"}
    err = validate_wizard_screen("update_wizard_state", patch, WizardState(wizard_screen="brief"))
    assert err is not None
    assert "artifact_kind" in err
    assert "data_mode" in err
    assert "task_objective" in err.split("may set only", 1)[1]


def test_screen_plan_takes_engine_models_and_budget() -> None:
    """Standard's one plan screen holds the engine, the models and the budget."""
    patch = {
        "optimizer_choice": "auto",
        "model_config": {"name": "openai/gpt-4o"},
        "reflection_model_config": {"name": "openai/gpt-4o"},
        "spending_limit_usd": 5.0,
    }
    assert validate_wizard_screen("update_wizard_state", patch, WizardState(wizard_screen="plan")) is None
    err = validate_wizard_screen("update_wizard_state", patch, WizardState(wizard_screen="optimizer"))
    assert err is not None
    assert "spending_limit_usd" in err


def test_screen_allows_only_screenless_fields_on_a_screen_without_fields() -> None:
    """The check and overview screens hold no field, so only the name may change."""
    state = WizardState(wizard_screen="check")
    assert validate_wizard_screen("update_wizard_state", {"job_name": "n"}, state) is None
    assert validate_wizard_screen("update_wizard_state", {"seed": 1}, state) is not None


def test_screen_lets_only_the_code_screens_write_code() -> None:
    """The Metric and Check screens write run and metric code; earlier screens may not."""
    code = {"run_code": "x", "metric_code": "y"}
    assert validate_wizard_screen("update_wizard_state", code, WizardState(wizard_screen="metric")) is None
    assert validate_wizard_screen("update_wizard_state", code, WizardState(wizard_screen="check")) is None
    assert validate_wizard_screen("update_wizard_state", code, WizardState(wizard_screen="brief")) is not None


@pytest.mark.asyncio
async def test_wrap_reports_previous_values_of_a_wizard_write() -> None:
    """A wizard write's tool_end carries what each field held before, for the chat's field card."""
    events: list[dict] = []
    tool = _wrap_tool_with_approval(
        _make_fake_tool("update_wizard_state", return_value={"wizard_state": {"run_code": "new"}}),
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=events.append,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=cast(WizardState, {**_READY, "wizard_screen": "metric", "run_code": "old"}),
    )
    await tool.func._async_body(run_code="new", metric_code="score", job_description="Sorts mail")
    end = next(e for e in events if e["event"] == "tool_end")
    assert end["data"]["status"] == "ok"
    assert end["data"]["result"]["previous"] == {
        "run_code": "old",
        "metric_code": _READY["metric_code"],
        "job_description": None,
    }
    assert end["data"]["result"]["wizard_state"] == {"run_code": "new"}


def test_cloned_setup_snapshot_holds_the_whole_setup() -> None:
    """A cloned run's agent sees every field, not only the screen on view."""
    state = cast(WizardState, {"wizard_screen": "brief", "cloned_run": True, "optimizer_choice": "auto"})
    assert screen_snapshot(state)["optimizer_choice"] == "auto"
    assert "optimizer_choice" not in screen_snapshot(WizardState(wizard_screen="brief", optimizer_choice="auto"))


def test_cloned_setup_signature_waits_for_the_user() -> None:
    """A cloned run's prompt says the setup arrived filled in, so the agent does not refill it."""
    assert CLONE_FOCUS in screen_signature("brief", cloned=True).instructions
    assert CLONE_FOCUS not in screen_signature("brief").instructions


def test_screen_signature_explains_automatic_check_messages() -> None:
    """The prompt tells the agent that a check-failure message comes from the wizard."""
    assert AUTOMATIC_TURN_PREFIX in screen_signature("check").instructions


def test_check_signature_holds_continue_until_the_check_passes() -> None:
    """The Check prompt tells the agent not to continue while the check runs or after it failed."""
    assert (
        "Do not call ``continue_to_next_screen`` while the check runs or after it failed."
        in screen_signature("check").instructions
    )


@pytest.mark.asyncio
async def test_wrap_refuses_off_screen_patch_without_calling_tool() -> None:
    """The wrapper turns an off-screen patch into an error observation."""
    events: list[dict] = []
    tool = _wrap_tool_with_approval(
        _make_fake_tool("update_wizard_state", return_value="should not run"),
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=events.append,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=cast(WizardState, {**_READY, "wizard_screen": "brief"}),
    )
    result = await tool.func._async_body(optimizer_choice="auto")
    assert result.startswith("Not on screen")
    end = next(e for e in events if e["event"] == "tool_end")
    assert end["data"]["status"] == "error"


def test_system_prompt_names_the_screen_rule() -> None:
    """The instructions tell the agent to edit only the screen on view."""
    prompt = GENERALIST_SYSTEM_PROMPT or ""
    assert "``wizard_state.wizard_screen`` names the wizard screen" in prompt


_FULL_SNAPSHOT: dict[str, Any] = {
    "job_name": "Tickets",
    "job_description": "d",
    "is_private": False,
    "task_objective": "Classify tickets",
    "task_background": "b",
    "artifact_kind": "dspy_program",
    "starting_version": "v1",
    "data_mode": "pairs",
    "dataset_ready": True,
    "dataset_columns": ["text", "label"],
    "input_fields": ["text"],
    "optimizer_choice": "auto",
    "model_config": {"name": "openai/gpt-4o"},
    "model_configured": True,
    "max_cost_cents": 500,
    "run_code": "def run(): ...",
    "metric_code": "def metric(): ...",
}


@pytest.mark.parametrize(
    ("screen", "expected"),
    [
        ("brief", {"task_objective", "task_background"}),
        ("artifact", {"artifact_kind", "starting_version", "task_objective"}),
        (
            "source",
            {"data_mode", "dataset_ready", "dataset_columns", "input_fields", "task_objective", "artifact_kind"},
        ),
        (
            "plan",
            {
                "optimizer_choice",
                "model_config",
                "model_configured",
                "max_cost_cents",
                "task_objective",
                "artifact_kind",
                "data_mode",
            },
        ),
        ("optimizer", {"optimizer_choice", "artifact_kind", "data_mode"}),
        ("models", {"model_config", "model_configured", "optimizer_choice"}),
        ("budget", {"max_cost_cents", "optimizer_choice", "model_config"}),
        ("metric", {"run_code", "metric_code", "task_objective", "artifact_kind", "starting_version", "input_fields"}),
        ("check", {"run_code", "metric_code", "input_fields"}),
    ],
)
def test_screen_snapshot_keeps_screen_fields_and_read_only_extras(screen: str, expected: set[str]) -> None:
    """A screen sees its own fields plus the values it depends on, nothing else."""
    trimmed = screen_snapshot(cast(WizardState, {**_FULL_SNAPSHOT, "wizard_screen": screen}))
    assert set(trimmed) == expected | {"wizard_screen", "job_name", "editable_fields"}
    assert trimmed["wizard_screen"] == screen


def test_screen_snapshot_shows_a_missing_run_name() -> None:
    """Every screen sees ``job_name``, null when unset, since writes wait on it."""
    unnamed = screen_snapshot(cast(WizardState, {"wizard_screen": "source", "task_objective": "Sort emails"}))
    assert unnamed["job_name"] is None
    named = screen_snapshot(cast(WizardState, {"wizard_screen": "source", "job_name": "Email sorter"}))
    assert named["job_name"] == "Email sorter"


def test_models_screen_sees_the_optimizer_read_only() -> None:
    """The models screen shows the optimizer but cannot write it."""
    state = cast(WizardState, {**_FULL_SNAPSHOT, "wizard_screen": "models"})
    assert screen_snapshot(state)["optimizer_choice"] == "auto"
    assert validate_wizard_screen("update_wizard_state", {"optimizer_choice": "gepa"}, state) is not None


def test_screen_snapshot_is_whole_without_a_screen() -> None:
    """Outside the wizard the agent still sees every value."""
    assert screen_snapshot(cast(WizardState, _FULL_SNAPSHOT)) == _FULL_SNAPSHOT


def test_screen_snapshot_shows_notes_only_from_earlier_screens() -> None:
    """A note reaches the screens after the one it was left on, not that one or earlier."""
    notes = [{"screen": "brief", "note": "keep it cheap"}, {"screen": "budget", "note": "cap at $5"}]
    state = {**_FULL_SNAPSHOT, "screen_notes": notes}
    assert (
        screen_snapshot(cast(WizardState, {**state, "wizard_screen": "brief"})).get("notes_from_earlier_screens")
        is None
    )
    budget = screen_snapshot(cast(WizardState, {**state, "wizard_screen": "budget"}))
    assert budget["notes_from_earlier_screens"] == ["brief: keep it cheap"]
    overview = screen_snapshot(cast(WizardState, {**state, "wizard_screen": "overview"}))
    assert overview["notes_from_earlier_screens"] == ["brief: keep it cheap", "budget: cap at $5"]
    assert "screen_notes" not in budget


def test_screen_snapshot_caps_client_notes() -> None:
    """Notes from the client are capped in count and length."""
    notes = [{"screen": "brief", "note": f"{i} " + "x" * 1100} for i in range(15)]
    state = cast(WizardState, {"wizard_screen": "budget", "screen_notes": notes})
    shown = screen_snapshot(state)["notes_from_earlier_screens"]
    assert len(shown) == 10
    assert shown[0].startswith("brief: 5 ")
    assert all(len(line) <= len("brief: ") + 1000 for line in shown)


def test_screen_snapshot_names_what_blocks_continue() -> None:
    """The Continue button's refusal reaches the agent."""
    state = cast(WizardState, {"wizard_screen": "brief", "screen_issue": "Describe the task first"})
    assert screen_snapshot(state)["continue_blocked_by"] == "Describe the task first"


def test_screen_snapshot_lists_the_fields_the_screen_takes() -> None:
    """The snapshot names the writable fields, so the agent need not guess them from refusals."""
    snapshot = screen_snapshot(cast(WizardState, {"wizard_screen": "brief", "job_name": "Tickets"}))
    assert snapshot["editable_fields"] == [
        "is_private",
        "job_description",
        "job_name",
        "task_background",
        "task_objective",
    ]
    assert "optimizer_choice" not in snapshot["editable_fields"]


def test_artifact_snapshot_carries_the_repository_and_github_link() -> None:
    """The artifact screen sees the chosen repository and whether GitHub is linked."""
    state = cast(
        WizardState,
        {
            **_READY,
            "wizard_screen": "artifact",
            "artifact_kind": "repo",
            "repository": "acme/support-bot",
            "github_connected": False,
        },
    )
    snapshot = screen_snapshot(state)
    assert snapshot["artifact_kind"] == "repo"
    assert snapshot["repository"] == "acme/support-bot"
    assert snapshot["github_connected"] is False
    assert "repository" in snapshot["editable_fields"]


def test_repo_target_needs_a_repository_not_a_starting_version() -> None:
    """A repository run completes the Task step with its repository alone."""
    repo = cast(
        WizardState,
        {"job_name": "Bot", "task_objective": "Fix flaky tests", "artifact_kind": "repo", "wizard_screen": "artifact"},
    )
    assert (
        validate_wizard_screen("update_wizard_state", {"artifact_kind": "repo", "repository": "acme/bot"}, repo) is None
    )
    assert _screen_problems("artifact", repo) == ["repository must be set as owner/name"]
    chosen = cast(WizardState, {**repo, "repository": "acme/bot"})
    assert _screen_problems("artifact", chosen) == []
    assert validate_wizard_patch_order({"data_mode": "single"}, chosen) is None


def test_repo_target_never_unlocks_the_unified_submit() -> None:
    """The wizard's own Run button submits a repository run, not the agent's submit tool."""
    assert _SUBMIT in tools_for(cast(WizardState, _READY))
    repo = cast(WizardState, {**_READY, "artifact_kind": "repo", "repository": "acme/bot"})
    assert _SUBMIT not in tools_for(repo)


def test_screen_prompts_forbid_unconfirmed_claims() -> None:
    """Both prompts tell the agent not to claim writes or screen moves a tool did not confirm."""
    assert "Say only what a tool result confirms." in (GENERALIST_SYSTEM_PROMPT or "")
    assert "Say only what a tool result confirms." in screen_signature("brief").instructions
    assert "the only kind new runs take" not in SCREEN_FOCUS["artifact"]


def test_screen_snapshot_of_a_clone_names_notes_and_what_blocks_continue() -> None:
    """A cloned run keeps its whole setup and still gets the named notes and blocker."""
    state = cast(
        WizardState,
        {
            "wizard_screen": "budget",
            "cloned_run": True,
            "optimizer_choice": "auto",
            "screen_issue": "Set a budget first",
            "screen_notes": [{"screen": "brief", "note": "keep it cheap"}],
        },
    )
    snapshot = screen_snapshot(state)
    assert snapshot["optimizer_choice"] == "auto"
    assert snapshot["continue_blocked_by"] == "Set a budget first"
    assert snapshot["notes_from_earlier_screens"] == ["brief: keep it cheap"]
    assert "screen_issue" not in snapshot
    assert "screen_notes" not in snapshot


@pytest.mark.parametrize(
    ("screen", "kept"),
    [
        ("brief", {"update_wizard_state"}),
        ("metric", {"update_wizard_state", "validate_code_validate_code_post"}),
        ("budget", {"update_wizard_state", "get_wallet_for_agent"}),
        ("overview", {"get_wallet_for_agent"}),
    ],
)
def test_screen_tools_keep_only_the_screens_own_tools(screen: str, kept: set[str]) -> None:
    """A screen agent gets that screen's tools only; other screens' tools are gone."""
    allowed = {
        "update_wizard_state",
        "validate_code_validate_code_post",
        "get_wallet_for_agent",
        "list_jobs",
        _SUBMIT,
    }
    assert screen_tools(allowed, WizardState(wizard_screen=screen)) == kept


def test_screen_tools_unchanged_without_a_screen() -> None:
    """With no ``wizard_screen`` every tool ``tools_for`` allows stays."""
    allowed = {"update_wizard_state", "validate_code_validate_code_post", "list_jobs"}
    assert screen_tools(allowed, WizardState()) == allowed


def _nav_tools(state: dict[str, Any], turn: _TurnAuthoringFlag | None = None) -> dict[str, dspy.Tool]:
    """Build the screen agent's local tools, keyed by name.

    Args:
        state: The wizard snapshot.
        turn: The turn flag; a fresh one when omitted.

    Returns:
        The tools by name.
    """
    tools = screen_nav_tools(cast(WizardState, state), turn or _TurnAuthoringFlag())
    return {tool.name: tool for tool in tools}


@pytest.mark.asyncio
async def test_remember_for_later_appends_a_capped_note() -> None:
    """A note lands whole in the wizard state with its screen; the oldest note drops out."""
    old = [{"screen": "brief", "note": str(i)} for i in range(10)]
    tools = _nav_tools({"wizard_screen": "artifact", "screen_notes": old})
    long_note = "keep   it\ncheap " + "x" * 900
    result = await tools["remember_for_later"].acall(note=long_note)
    notes = result["wizard_state"]["screen_notes"]
    assert len(notes) == 10
    assert notes[0] == {"screen": "brief", "note": "1"}
    assert notes[-1] == {"screen": "artifact", "note": "keep it cheap " + "x" * 900}


@pytest.mark.asyncio
async def test_remember_for_later_refuses_a_note_over_the_limit() -> None:
    """A note over the limit is refused with its length, never cut."""
    result = await _nav_tools({"wizard_screen": "brief"})["remember_for_later"].acall(note="x" * 1001)
    assert isinstance(result, str)
    assert "1001" in result
    assert "1000" in result


@pytest.mark.asyncio
async def test_continue_refuses_a_note_over_the_limit() -> None:
    """Continue does not move on while its note is too long."""
    state = {**_READY, "wizard_screen": "brief"}
    result = await _nav_tools(state)["continue_to_next_screen"].acall(note="x" * 1001)
    assert isinstance(result, str)
    assert "1000" in result


@pytest.mark.asyncio
async def test_remember_for_later_refuses_an_empty_note() -> None:
    """An empty note writes nothing."""
    result = await _nav_tools({"wizard_screen": "brief"})["remember_for_later"].acall(note="  ")
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_continue_refused_while_the_screen_fails_validation() -> None:
    """Continue on an empty brief returns the errors, not a move."""
    result = await _nav_tools({"wizard_screen": "brief"})["continue_to_next_screen"].acall()
    assert isinstance(result, str)
    assert "task_objective" in result


@pytest.mark.asyncio
async def test_continue_refused_while_the_wizard_blocks_continue() -> None:
    """The wizard's own Continue check refuses the move too."""
    state = {**_READY, "wizard_screen": "brief", "screen_issue": "Name the run"}
    result = await _nav_tools(state)["continue_to_next_screen"].acall()
    assert isinstance(result, str)
    assert "Name the run" in result


@pytest.mark.asyncio
async def test_continue_judges_this_turns_writes() -> None:
    """A field written earlier this turn counts toward the screen being done."""
    turn = _TurnAuthoringFlag()
    turn.wizard_patch["task_objective"] = "Sort tickets"
    result = await _nav_tools({"wizard_screen": "brief", "screen_issue": "stale"}, turn)[
        "continue_to_next_screen"
    ].acall()
    assert result["screen_nav"] == {"action": "continue", "from": "brief"}


@pytest.mark.asyncio
async def test_continue_returns_to_the_screen_a_go_back_left() -> None:
    """After a go-back edit, Continue jumps back once every screen between is done."""
    state = {**_READY, "wizard_screen": "optimizer", "return_screen": "budget"}
    result = await _nav_tools(state)["continue_to_next_screen"].acall()
    assert result["screen_nav"] == {"action": "continue", "from": "optimizer", "to": "budget"}


@pytest.mark.asyncio
async def test_continue_moves_one_screen_when_a_screen_between_is_not_done() -> None:
    """A screen between that still needs work keeps the plain one-screen move."""
    state = {**_READY, "reflection_model_config": {}, "wizard_screen": "optimizer", "return_screen": "budget"}
    result = await _nav_tools(state)["continue_to_next_screen"].acall()
    assert result["screen_nav"] == {"action": "continue", "from": "optimizer"}


@pytest.mark.parametrize("back", ["models", "brief", "nowhere"])
@pytest.mark.asyncio
async def test_continue_ignores_a_return_screen_that_is_not_a_jump_ahead(back: str) -> None:
    """The next screen, an earlier one or an unknown one gives no ``to``."""
    state = {**_READY, "wizard_screen": "optimizer", "return_screen": back}
    result = await _nav_tools(state)["continue_to_next_screen"].acall()
    assert result["screen_nav"] == {"action": "continue", "from": "optimizer"}


def test_snapshot_tells_the_agent_where_continue_returns() -> None:
    """The screen agent sees the pending return, not the raw field."""
    snapshot = screen_snapshot(cast(WizardState, {**_READY, "wizard_screen": "optimizer", "return_screen": "budget"}))
    assert snapshot["continue_returns_to"] == "budget"
    assert "return_screen" not in snapshot


@pytest.mark.asyncio
async def test_continue_moves_on_and_saves_its_note() -> None:
    """A done screen moves on, and the optional note is kept like ``remember_for_later``."""
    state = {**_READY, "wizard_screen": "brief"}
    result = await _nav_tools(state)["continue_to_next_screen"].acall(note="keep it cheap")
    assert result["screen_nav"] == {"action": "continue", "from": "brief"}
    assert result["wizard_state"]["screen_notes"] == [{"screen": "brief", "note": "keep it cheap"}]


@pytest.mark.asyncio
async def test_continue_never_leaves_the_overview() -> None:
    """The last screen submits instead."""
    result = await _nav_tools({**_READY, "wizard_screen": "overview"})["continue_to_next_screen"].acall()
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_go_back_only_to_an_earlier_screen() -> None:
    """Going back names an earlier screen and carries the request."""
    tools = _nav_tools({"wizard_screen": "budget"})
    result = await tools["go_back_to_screen"].acall(screen_id="brief", request="Make it about refunds")
    assert result["screen_nav"] == {
        "action": "back",
        "from": "budget",
        "screen": "brief",
        "request": "Make it about refunds",
    }
    assert isinstance(await tools["go_back_to_screen"].acall(screen_id="metric", request="x"), str)
    assert isinstance(await tools["go_back_to_screen"].acall(screen_id="brief", request=" "), str)


@pytest.mark.parametrize("screen", list(SCREEN_FOCUS))
def test_screen_signature_is_short_and_names_the_screen(screen: str) -> None:
    """Each screen gets its own short instructions, not the full prompt."""
    instructions = screen_signature(screen).instructions
    assert f"The screen on view is ``{screen}``" in instructions
    assert SCREEN_FOCUS[screen] in instructions
    assert "remember_for_later" in instructions
    assert len(instructions) < len(GENERALIST_SYSTEM_PROMPT or "")


def test_source_signature_sets_the_column_roles() -> None:
    """The Data prompt tells the agent to set the column roles, so it fills the roles card."""
    assert "``set_column_roles``" in SCREEN_FOCUS["source"]
    assert SCREEN_FOCUS["source"] in screen_signature("source").instructions


def test_screen_signature_names_the_reply_language() -> None:
    """The screen prompt names the language outright, so Hebrew tool results can't pull it."""
    instructions = screen_signature("source", "English").instructions
    assert "every user-facing tool argument in\n  English." in instructions
    assert "often Hebrew" in instructions


@pytest.mark.asyncio
async def test_sample_listing_carries_the_reply_language() -> None:
    """Listing the Hebrew samples reminds the agent which language to write in."""

    async def listing() -> dict:
        """Return a one-sample catalog."""
        return {"samples": [{"sample_id": "sentiment-he"}]}

    tool = note_sample_language(dspy.Tool(listing, name="list_sample_datasets_datasets_samples_get"), "English")
    result = await tool.acall()
    assert result["samples"] == {"samples": [{"sample_id": "sentiment-he"}]}
    assert "Describe them in English" in result["note"]
    hebrew = note_sample_language(dspy.Tool(listing, name="list_sample_datasets_datasets_samples_get"), "Hebrew")
    assert "note" not in await hebrew.acall()


@pytest.mark.parametrize(
    ("patch", "blocked"),
    [
        ({"spending_limit_usd": 3}, True),
        ({"spending_limit_usd": 3.7}, False),
        ({"max_cost_cents": 300}, True),
        ({"spending_limit_usd": None}, False),
        ({}, False),
    ],
)
def test_budget_screen_refuses_a_limit_below_the_estimate(patch: dict[str, Any], blocked: bool) -> None:
    """Continue on the budget screen names the floor when the limit sits under it."""
    problems = _screen_problems("budget", cast(WizardState, {"spending_floor_cents": 370, **patch}))
    assert bool(problems) is blocked
    if blocked:
        assert "$3.70" in problems[0]


@pytest.mark.parametrize("screen", ["plan", "budget"])
def test_spending_screens_ask_the_user_for_the_limit(screen: str) -> None:
    """Both screens that hold the spending limit tell the agent to ask, not pick one."""
    focus = SCREEN_FOCUS[screen]
    assert "only the user decides it" in focus
    assert "spending_floor_cents" in focus
    assert "wait for the answer" in focus
    assert "do not ask whether to continue" in focus


def _wizard_tool() -> dspy.Tool:
    """Build an ``update_wizard_state`` stand-in with a field per screen.

    Returns:
        A ``dspy.Tool`` whose arguments span several wizard screens.
    """

    def update_wizard_state(
        job_name: str = "",
        job_description: str = "",
        is_private: bool = False,
        task_objective: str = "",
        artifact_kind: str = "",
        optimizer_choice: str = "",
        spending_limit_usd: float = 0.0,
    ) -> str:
        """Patch the wizard."""
        return "ok"

    return dspy.Tool(update_wizard_state)


def test_scope_wizard_tool_names_only_current_screen_fields() -> None:
    """On the budget screen the tool takes the budget plus the screenless fields."""
    tool = scope_wizard_tool(_wizard_tool(), WizardState(wizard_screen="budget"))
    assert set(tool.args) == {"spending_limit_usd", "job_name", "job_description", "is_private"}
    assert "set only spending_limit_usd" in tool.desc
    assert "optimizer_choice" not in tool.desc


def test_scope_wizard_tool_unchanged_without_a_screen() -> None:
    """Outside the wizard the tool keeps every argument."""
    tool = scope_wizard_tool(_wizard_tool(), WizardState())
    assert "optimizer_choice" in tool.args
    assert "artifact_kind" in tool.args


def test_scope_wizard_tool_ignores_other_tools() -> None:
    """Only ``update_wizard_state`` is narrowed."""
    other = _make_fake_tool("list_jobs")
    before = dict(other.args)
    assert scope_wizard_tool(other, WizardState(wizard_screen="brief")).args == before


def test_screen_prompt_keeps_screenless_fields_to_explicit_asks() -> None:
    """Screen agents set name, description and privacy only when asked."""
    instructions = screen_signature("brief").instructions
    assert "Set them only when the user asks for them this turn" in instructions
    assert "go_back_to_screen" in instructions


def test_order_ignores_unmapped_fields() -> None:
    """A patch with no step-mapped fields passes (code fields are rejected elsewhere)."""
    assert validate_wizard_patch_order({"run_code": "x"}, WizardState()) is None


def test_always_tools_include_discovery_and_post_submit() -> None:
    """The always-on toolset includes discovery and post-submit lifecycle tools."""
    allowed = tools_for(WizardState())
    assert "list_models_for_agent" in allowed
    assert "get_registry_snapshot_registry_get" in allowed
    assert "list_jobs_optimizations_get" in allowed
    assert "cancel_job_optimizations" in allowed
    assert "rename_job_optimizations" in allowed


def test_read_only_reach_tools_always_available() -> None:
    """Wallet and tagging-session reads are always-on and never gate."""
    allowed = tools_for(WizardState())
    assert "get_wallet_for_agent" in allowed
    assert "list_tagging_sessions_for_agent" in allowed
    for name in ("get_wallet_for_agent", "list_tagging_sessions_for_agent"):
        assert _needs_approval(name, "ask") is False


def test_dataset_library_tools_always_available() -> None:
    """The saved-dataset library read + by-reference picker are always exposed."""
    allowed = tools_for(WizardState())
    assert "list_datasets_for_agent" in allowed
    assert "request_user_dataset_from_library" in allowed


def test_pair_inference_trigger_always_available() -> None:
    """The pair inference trigger for stored grid runs is always exposed and never gates."""
    assert "request_user_pair_inference" in tools_for(WizardState())
    assert _needs_approval("request_user_pair_inference", "ask") is False


def test_yolo_never_gates() -> None:
    """Yolo trust-mode never gates any tool."""
    for name in ("delete_job_optimizations", "submit_unified_run_optimize_post", "rename_job_optimizations"):
        assert _needs_approval(name, "yolo") is False


def test_ask_gates_every_mutation() -> None:
    """Ask trust-mode gates every mutating tool."""
    assert _needs_approval("delete_job_optimizations", "ask") is True
    assert _needs_approval("rename_job_optimizations", "ask") is True
    assert _needs_approval("submit_unified_run_optimize_post", "ask") is True


def test_auto_safe_gates_only_destructive() -> None:
    """Auto-safe gates only destructive operations."""
    assert _needs_approval("rename_job_optimizations", "auto_safe") is False
    assert _needs_approval("toggle_pin_job_optimizations", "auto_safe") is False
    assert _needs_approval("delete_job_optimizations", "auto_safe") is True
    assert _needs_approval("submit_unified_run_optimize_post", "auto_safe") is True


def test_screen_agent_never_asks() -> None:
    """A screen agent works without approvals in every trust mode."""
    for mode in ("ask", "auto_safe", "yolo"):
        assert _screen_needs_approval("update_wizard_state", mode) is False
        assert _screen_needs_approval("stage_sample_dataset_datasets_samples", mode) is False


def _make_fake_tool(name: str, return_value: str = "ok") -> dspy.Tool:
    """Build a ``dspy.Tool`` whose async ``func`` returns the given value."""

    async def func(**kwargs):
        """Return the fixed value."""
        return return_value

    return dspy.Tool(func=func, name=name, desc="test tool", args={}, arg_types={}, arg_desc={})


@pytest.mark.asyncio
async def test_wrap_bypasses_when_no_approval_needed() -> None:
    """Wrapped tool runs straight through when no approval is needed."""
    events: list[dict] = []
    registry = ApprovalRegistry()
    tool = _wrap_tool_with_approval(
        _make_fake_tool("rename_job_optimizations", return_value="renamed"),
        trust_mode="auto_safe",
        registry=registry,
        emit=events.append,
        outer_loop=asyncio.get_running_loop(),
    )
    # Drive the async body directly: the sync ``__call__`` schedules onto
    # ``outer_loop`` via ``run_coroutine_threadsafe`` for DSPy's worker-thread
    # dispatch, but inside the test loop we exercise the same logic by awaiting
    # ``_async_body`` so we don't deadlock blocking on the running loop.
    result = await tool.func._async_body()
    assert result == "renamed"
    event_names = [e["event"] for e in events]
    assert "pending_approval" not in event_names
    assert event_names == ["tool_start", "tool_end"]


@pytest.mark.asyncio
async def test_wrap_emits_pending_and_runs_on_approve() -> None:
    """Wrapped tool emits ``pending_approval`` and runs once approved."""
    events: list[dict] = []
    registry = ApprovalRegistry()
    tool = _wrap_tool_with_approval(
        _make_fake_tool("delete_job_optimizations", return_value="deleted"),
        trust_mode="ask",
        registry=registry,
        emit=events.append,
        outer_loop=asyncio.get_running_loop(),
    )
    call_task = asyncio.create_task(tool.func._async_body())
    for _ in range(20):
        await asyncio.sleep(0)
        if any(e["event"] == "pending_approval" for e in events):
            break
    pending = next((e for e in events if e["event"] == "pending_approval"), None)
    assert pending is not None
    call_id = pending["data"]["id"]
    assert registry.resolve(call_id, True) is True
    result = await call_task
    assert result == "deleted"
    resolved = next((e for e in events if e["event"] == "approval_resolved"), None)
    assert resolved is not None
    assert resolved["data"]["approved"] is True


@pytest.mark.asyncio
async def test_denial_returns_observation_not_exception() -> None:
    """A denied approval surfaces a string observation instead of raising."""
    events: list[dict] = []
    registry = ApprovalRegistry()
    tool = _wrap_tool_with_approval(
        _make_fake_tool("submit_unified_run_optimize_post", return_value="should not run"),
        trust_mode="ask",
        registry=registry,
        emit=events.append,
        outer_loop=asyncio.get_running_loop(),
    )
    call_task = asyncio.create_task(tool.func._async_body())
    for _ in range(20):
        await asyncio.sleep(0)
        if events:
            break
    call_id = events[0]["data"]["id"]
    registry.resolve(call_id, False)
    result = await call_task
    assert result == "User declined"


def _make_recording_tool(name: str) -> tuple[dspy.Tool, dict]:
    """Build a ``dspy.Tool`` whose async ``func`` records the kwargs it receives.

    Args:
        name: Registered tool name.

    Returns:
        The tool and the dict its ``func`` populates with received kwargs.
    """
    seen: dict = {}

    async def func(**kwargs):
        """Record the kwargs and return ``ok``."""
        seen.update(kwargs)
        return "ok"

    tool = dspy.Tool(func=func, name=name, desc="test tool", args={}, arg_types={}, arg_desc={})
    return tool, seen


@pytest.mark.asyncio
async def test_submit_fills_unified_payload_from_snapshot() -> None:
    """Submit builds the unified request from the wizard snapshot."""
    tool, seen = _make_recording_tool(_SUBMIT)
    _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        staged_dataset_id="ds_1",
        wizard_state=cast(
            WizardState,
            {
                **_READY,
                "task_background": "Tier-1 support",
                "input_fields": ["q"],
                "expected_field": "a",
                "optimizer_choice": "gepa",
                "split_fractions": {"train": 0.6, "val": 0.2, "test": 0.2},
                "seed": 7,
            },
        ),
    )
    await tool.func._async_body()
    assert seen["name"] == "My run"
    assert seen["task"] == {
        "objective": "Answer support questions",
        "background": "Tier-1 support",
        "artifact": {"kind": "dspy_program", "starting_version": "You are a helpful agent."},
    }
    assert seen["data"] == {
        "mode": "pairs",
        "staged_dataset_id": "ds_1",
        "input_fields": ["q"],
        "expected_field": "a",
        "split": {"train": 0.6, "val": 0.2, "test": 0.2},
    }
    assert seen["optimizer"] == {"choice": "gepa"}
    assert seen["models"] == {"reflection_model": {"name": "openai/gpt-4o-mini"}}
    assert seen["workspace"] == {"run_code": _READY["run_code"], "metric_code": _READY["metric_code"]}
    assert seen["seed"] == 7
    assert seen["is_private"] is True


@pytest.mark.asyncio
async def test_submit_injects_snapshot_code_over_agent_supplied() -> None:
    """Workspace code always comes from the snapshot, discarding what the agent typed."""
    tool, seen = _make_recording_tool(_SUBMIT)
    _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=cast(WizardState, {"run_code": "good_run", "metric_code": "good_metric"}),
    )
    await tool.func._async_body(workspace={"run_code": "BROKEN {", "metric_code": "bad", "install_command": "pip x"})
    assert seen["workspace"] == {"run_code": "good_run", "metric_code": "good_metric", "install_command": "pip x"}


@pytest.mark.asyncio
async def test_submit_keeps_agent_supplied_values() -> None:
    """Agent-supplied name, description, seed and optimizer win over the snapshot."""
    tool, seen = _make_recording_tool(_SUBMIT)
    _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=cast(
            WizardState,
            {"job_name": "wizard", "job_description": "wizard", "seed": 1, "optimizer_choice": "gepa"},
        ),
    )
    await tool.func._async_body(name="agent", description="agent", seed=2, optimizer={"choice": "best_of_n"})
    assert seen["name"] == "agent"
    assert seen["description"] == "agent"
    assert seen["seed"] == 2
    assert seen["optimizer"] == {"choice": "best_of_n"}


@pytest.mark.asyncio
async def test_submit_injects_wizard_description() -> None:
    """Submit carries the wizard's job_description — stripped and clipped to 280."""
    tool, seen = _make_recording_tool(_SUBMIT)
    _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=cast(WizardState, {"job_description": "  " + "x" * 300 + "  "}),
    )
    await tool.func._async_body()
    assert seen["description"] == "x" * 280


@pytest.mark.asyncio
async def test_submit_inline_cases_suppress_staged_id() -> None:
    """Cases the agent passes inline are not overridden by the staged dataset id."""
    tool, seen = _make_recording_tool(_SUBMIT)
    _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        staged_dataset_id="ds_1",
        wizard_state=cast(WizardState, {"data_mode": "single"}),
    )
    await tool.func._async_body(data={"cases": [{"input": {"q": "hi"}}]})
    assert seen["data"] == {"cases": [{"input": {"q": "hi"}}], "mode": "single"}


@pytest.mark.asyncio
async def test_submit_defaults_to_private() -> None:
    """Submit forces ``is_private`` to private when the snapshot never set it."""
    tool, seen = _make_recording_tool(_SUBMIT)
    _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=cast(WizardState, {}),
    )
    await tool.func._async_body(is_private=False)
    assert seen["is_private"] is True


@pytest.mark.asyncio
async def test_submit_respects_explicit_public() -> None:
    """A snapshot ``is_private=False`` (user asked for public) reaches submit."""
    tool, seen = _make_recording_tool(_SUBMIT)
    _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=cast(WizardState, {"is_private": False}),
    )
    await tool.func._async_body()
    assert seen["is_private"] is False


@pytest.mark.asyncio
async def test_non_submit_tool_does_not_inject_code() -> None:
    """Code injection is scoped to the submit tool; other tools are untouched."""
    tool, seen = _make_recording_tool("update_wizard_state")
    _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=cast(WizardState, {"run_code": "snap", "metric_code": "snap"}),
    )
    await tool.func._async_body(job_name="x")
    assert "workspace" not in seen


@pytest.mark.asyncio
async def test_profile_injects_staged_dataset_id() -> None:
    """Profiling a staged dataset gets the opaque id injected so the backend rehydrates."""
    tool, seen = _make_recording_tool("profile_datasets_profile_post")
    _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        staged_dataset_id="ds_123",
        wizard_state=cast(WizardState, {}),
    )
    await tool.func._async_body(column_mapping={"inputs": {}, "outputs": {}})
    assert seen["staged_dataset_id"] == "ds_123"


@pytest.mark.asyncio
async def test_profile_inline_dataset_not_overridden_by_staged_id() -> None:
    """An inline dataset on the profile call suppresses staged-id injection."""
    tool, seen = _make_recording_tool("profile_datasets_profile_post")
    _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        staged_dataset_id="ds_123",
        wizard_state=cast(WizardState, {}),
    )
    await tool.func._async_body(dataset=[{"q": "x"}], column_mapping={"inputs": {}, "outputs": {}})
    assert "staged_dataset_id" not in seen


def test_registry_resolve_unknown_returns_false() -> None:
    """Resolving an unknown call id returns ``False``."""
    registry = ApprovalRegistry()
    assert registry.resolve("does-not-exist", True) is False


def test_submit_arg_extractor_streams_value_incrementally() -> None:
    """Incremental chunks of a submit tool_calls payload yield growing deltas."""
    ext = _SubmitArgExtractor("assistant_message")
    deltas: list[str] = []
    for chunk in (
        '{"tool_calls": [',
        '{"name": "submit", "args": ',
        '{"assistant_message": "',
        "Shal",
        "om, ",
        "Gilad",
        '!"}}]}',
    ):
        delta = ext.feed(chunk)
        if delta:
            deltas.append(delta)
    assert "".join(deltas) == "Shalom, Gilad!"


def test_submit_arg_extractor_ignores_non_submit_calls() -> None:
    """A non-submit tool call produces no delta, and reset clears the buffer."""
    ext = _SubmitArgExtractor("assistant_message")
    assert ext.feed('{"tool_calls": [{"name": "list_models", "args": {}}]}') is None
    ext.reset()
    delta = ext.feed('{"tool_calls": [{"name": "submit", "args": {"assistant_message": "Done!"}}]}')
    assert delta == "Done!"


def test_submit_arg_extractor_handles_malformed_json() -> None:
    """A partial / malformed chunk returns ``None`` without raising."""
    ext = _SubmitArgExtractor("reply")
    assert ext.feed('{"tool_calls": [{"name": "submit') is None


def test_submit_arg_extractor_picks_submit_among_parallel_calls() -> None:
    """Parallel tool calls including submit still resolve to the submit arg."""
    ext = _SubmitArgExtractor("reply")
    parallel = '{"tool_calls": [{"name": "foo", "args": {}}, {"name": "submit", "args": {"reply": "yo"}}]}'
    assert ext.feed(parallel) == "yo"


def test_submit_arg_extractor_is_idempotent_on_repeat_feed() -> None:
    """Re-feeding the same buffer or an empty chunk yields no new delta."""
    ext = _SubmitArgExtractor("reply")
    full = '{"tool_calls": [{"name": "submit", "args": {"reply": "hi"}}]}'
    assert ext.feed(full) == "hi"
    assert ext.feed("") is None


@pytest.mark.asyncio
async def test_submit_runs_through_the_wrapper() -> None:
    """A submit with the turn state shared reaches the underlying tool."""
    flag = _TurnAuthoringFlag()
    submit, submit_seen = _make_recording_tool("submit_unified_run_optimize_post")
    _wrap_tool_with_approval(
        submit,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=cast(WizardState, {}),
        authoring_flag=flag,
    )
    result = await submit.func._async_body(name="My run")
    assert result == "ok"
    assert submit_seen["name"] == "My run"


@pytest.mark.asyncio
async def test_continue_after_a_repository_only_write_counts_a_repository_run() -> None:
    """A repository write without ``artifact_kind`` lets Continue pass the artifact screen."""
    flag = _TurnAuthoringFlag()
    state = {
        "wizard_screen": "artifact",
        "job_name": "My run",
        "task_objective": "Sort tickets",
        "artifact_kind": "dspy_program",
    }
    update, _ = _make_recording_tool("update_wizard_state")
    _wrap_tool_with_approval(
        update,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=cast(WizardState, state),
        authoring_flag=flag,
    )
    assert await update.func._async_body(repository="acme/app") == "ok"
    result = await _nav_tools(state, flag)["continue_to_next_screen"].acall()
    assert result["screen_nav"] == {"action": "continue", "from": "artifact"}


def test_system_prompt_sends_code_requests_to_the_metric_screen() -> None:
    """Outside the wizard the prompt points code requests at the wizard's Metric screen."""
    prompt = " ".join((GENERALIST_SYSTEM_PROMPT or "").split())
    assert "send them to the Metric screen" in prompt


def _approval_engine():
    """In-memory shared-across-threads SQLite engine with the ORM schema."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def test_resolve_or_persist_without_engine_rejects_unknown_id() -> None:
    """Store-less registries keep the old contract: unknown id is unresolved."""
    registry = ApprovalRegistry()
    assert registry.resolve_or_persist("nope", True) is False


@pytest.mark.asyncio
async def test_confirm_on_another_replica_reaches_waiting_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decision persisted by one replica resolves another replica's wait loop."""
    monkeypatch.setattr(generalist_module, "_DURABLE_POLL_SECONDS", 0.01)
    engine = _approval_engine()
    streaming = ApprovalRegistry()
    streaming.bind_engine(engine)
    confirming = ApprovalRegistry()
    confirming.bind_engine(engine)

    fut = streaming.register("call-cross")
    # The confirming replica holds no future for this id — it must persist.
    assert confirming.resolve_or_persist("call-cross", True) is True
    assert await streaming.wait_for_decision("call-cross", fut) is True
    # The decision row is consumed on delivery.
    assert streaming._take_durable("call-cross") is None


@pytest.mark.asyncio
async def test_wait_for_decision_expires_as_declined(monkeypatch: pytest.MonkeyPatch) -> None:
    """A confirm that never arrives expires the wait as a decline, not a hang."""
    monkeypatch.setattr(generalist_module, "APPROVAL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(generalist_module, "_DURABLE_POLL_SECONDS", 0.01)
    registry = ApprovalRegistry()
    fut = registry.register("call-lost")
    assert await registry.wait_for_decision("call-lost", fut) is False


@pytest.mark.asyncio
async def test_local_resolve_still_wins_instantly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-process fast path resolves without touching the durable store."""
    registry = ApprovalRegistry()
    fut = registry.register("call-local")
    task = asyncio.create_task(registry.wait_for_decision("call-local", fut))
    await asyncio.sleep(0)
    assert registry.resolve_or_persist("call-local", False) is True
    assert await task is False


def test_agent_error_payload_flags_litellm_context_overflow() -> None:
    """litellm's typed context-window error carries the machine code."""
    exc = litellm.ContextWindowExceededError(
        "The prompt exceeds the model limit", model="gpt-4o", llm_provider="openai"
    )
    payload = _agent_error_payload(exc)
    assert payload["code"] == "context_too_long"
    assert payload["error"]


def test_agent_error_payload_flags_provider_overflow_message() -> None:
    """An untyped provider 400 mentioning context length is classified too."""
    exc = RuntimeError(
        "BadRequestError: This model's maximum context length is 128000 tokens, however you requested 191694 tokens"
    )
    assert _agent_error_payload(exc)["code"] == "context_too_long"


def test_agent_error_payload_walks_groups_and_causes() -> None:
    """The classifier sees through exception groups and __cause__ chains."""
    inner = ValueError("prompt is too long: 250000 tokens > 200000 maximum")
    wrapper = RuntimeError("agent turn failed")
    wrapper.__cause__ = inner
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [wrapper])
    assert _agent_error_payload(group)["code"] == "context_too_long"


def test_agent_error_payload_plain_error_has_no_code() -> None:
    """Unclassified failures keep the text-only payload."""
    payload = _agent_error_payload(RuntimeError("boom"))
    assert payload == {"error": "RuntimeError: boom"}


def test_always_on_catalog_and_lifecycle_tools_are_reachable() -> None:
    """Sample datasets, the engine and optimizer catalogs, and pause/resume/restart need no wizard progress."""
    allowed = tools_for(cast(WizardState, {}))
    assert {
        "list_sample_datasets_datasets_samples_get",
        "stage_sample_dataset_datasets_samples",
        "blackbox_engines_blackbox_engines_get",
        "optimizer_catalog_optimizers_get",
        "pause_job_optimizations",
        "resume_job_optimizations",
        "restart_job_optimizations",
    } <= allowed


def test_trust_gating_of_lifecycle_and_submit_tools() -> None:
    """Balance-spending tools gate in auto-safe; pause and samples only gate in ask."""
    for tool in ("resume_job_optimizations", "restart_job_optimizations", _SUBMIT):
        assert _needs_approval(tool, "auto_safe") is True
        assert _needs_approval(tool, "yolo") is False
    for tool in ("pause_job_optimizations", "stage_sample_dataset_datasets_samples"):
        assert _needs_approval(tool, "ask") is True
        assert _needs_approval(tool, "auto_safe") is False


def test_approval_key_binds_to_owner() -> None:
    """Different owners derive different keys; an owner-less caller keeps the bare id."""
    assert approval_key("abc123", None) == "abc123"
    alice = approval_key("abc123", "alice")
    assert alice.startswith("abc123.")
    assert alice != approval_key("abc123", "bob")
    assert alice == approval_key("abc123", "alice")
    assert len(alice) <= 45


@pytest.mark.asyncio
async def test_pending_approval_resolves_only_with_owner_key() -> None:
    """The bare call id from the event cannot resolve an owner-bound approval."""
    events: list[dict] = []
    registry = ApprovalRegistry()
    tool = _wrap_tool_with_approval(
        _make_fake_tool("delete_job_optimizations", return_value="deleted"),
        trust_mode="ask",
        registry=registry,
        emit=events.append,
        outer_loop=asyncio.get_running_loop(),
        approval_owner="alice",
    )
    call_task = asyncio.create_task(tool.func._async_body())
    for _ in range(20):
        await asyncio.sleep(0)
        if any(e["event"] == "pending_approval" for e in events):
            break
    call_id = next(e for e in events if e["event"] == "pending_approval")["data"]["id"]
    assert registry.resolve(call_id, True) is False
    assert registry.resolve(approval_key(call_id, "bob"), True) is False
    assert registry.resolve(approval_key(call_id, "alice"), True) is True
    assert await call_task == "deleted"


@pytest.mark.asyncio
async def test_sample_staging_keeps_an_existing_job_name() -> None:
    """A staged sample names an unnamed run but never renames a named one."""
    loop = asyncio.get_running_loop()
    staged = '{"sample_id": "s", "wizard_state": {"job_name": "Sample", "dataset_ready": true}}'

    async def emitted_patch(state: dict[str, Any], *, name_first: bool) -> dict[str, Any]:
        """Stage a sample and return the ``wizard_state`` patch the panel receives.

        Args:
            state: The turn-start wizard snapshot.
            name_first: Whether the agent names the run earlier in the same turn.

        Returns:
            The patch carried by the staging tool's ``tool_end`` event.
        """
        events: list[dict[str, Any]] = []
        flag = _TurnAuthoringFlag()

        def wrap(tool: dspy.Tool) -> dspy.Tool:
            """Wrap a tool on the shared turn flag.

            Args:
                tool: The tool to wrap.

            Returns:
                The wrapped tool.
            """
            return _wrap_tool_with_approval(
                tool,
                trust_mode="yolo",
                registry=ApprovalRegistry(),
                emit=events.append,
                outer_loop=loop,
                authoring_flag=flag,
                wizard_state=cast(WizardState, state),
            )

        if name_first:
            await wrap(_make_fake_tool("update_wizard_state")).func._async_body(job_name="Mine")
        await wrap(_make_fake_tool("stage_sample_dataset_datasets_samples", return_value=staged)).func._async_body(
            sample_id="s"
        )
        return events[-1]["data"]["result"]["wizard_state"]

    assert await emitted_patch({}, name_first=False) == {"job_name": "Sample", "dataset_ready": True}
    assert await emitted_patch({"job_name": "Mine"}, name_first=False) == {"dataset_ready": True}
    assert await emitted_patch({}, name_first=True) == {"dataset_ready": True}


def test_system_prompt_covers_new_capabilities() -> None:
    """The prompt names the unified setup fields and tools the agent now supports."""
    prompt = GENERALIST_SYSTEM_PROMPT or ""
    for needle in ("optimizer_catalog", "artifact_kind", "data_mode", "run_code", "validate_datasets", "restart"):
        assert needle in prompt


class _ScriptedLM(dspy.BaseLM):
    """An LM that records every request and answers with scripted native tool calls."""

    def __init__(self, script: list[tuple[str, dict[str, Any]]]) -> None:
        """Store the script.

        Args:
            script: One ``(tool name, arguments)`` pair per expected LM call.
        """
        super().__init__(model="scripted")
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []

    @property
    def supports_function_calling(self) -> bool:
        """Report native tool-call support."""
        return True

    @property
    def supports_reasoning(self) -> bool:
        """Report native reasoning support."""
        return True

    def forward(self, prompt=None, messages=None, **kwargs):
        """Record the request and return the next scripted tool call.

        Args:
            prompt: Unused legacy prompt.
            messages: The rendered chat messages.
            **kwargs: Provider kwargs, including ``tools``.

        Returns:
            An OpenAI-shaped response carrying one tool call.
        """
        self.requests.append({"messages": messages, **kwargs})
        name, args = self._script.pop(0)
        call = {
            "id": f"call_{len(self.requests)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        return litellm.ModelResponse(
            model="scripted",
            choices=[{"index": 0, "finish_reason": "tool_calls", "message": message}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


class _Spec:
    """A minimal MCP tool listing entry."""

    def __init__(self, name: str) -> None:
        """Name the tool.

        Args:
            name: The tool's MCP name.
        """
        self.name = name
        self.description = "test tool"
        self.inputSchema = {"type": "object", "properties": {}}


class _Listing:
    """What a fake MCP session's ``list_tools`` returns."""

    def __init__(self, names: list[str]) -> None:
        """Wrap the tool specs.

        Args:
            names: The listed tool names.
        """
        self.tools = [_Spec(name) for name in names]


class _FakeSession:
    """An MCP session that only lists tools."""

    def __init__(self, names: list[str]) -> None:
        """Remember which tools to list.

        Args:
            names: The listed tool names.
        """
        self._names = names

    async def list_tools(self) -> _Listing:
        """List the configured tools."""
        return _Listing(self._names)


async def test_turn_replays_history_natively_and_streams_the_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn runs on the conversation loop: earlier turns and their tool trace precede the new one as messages.

    Also covers the native-tool-call gap the CLI bridge used to close: a
    ``null`` optional never reaches the gated tool.
    """
    # The local .env may carry a Firecrawl key; the tool list below is the MCP roster alone.
    monkeypatch.setattr(settings, "firecrawl_api_key", None)
    tool, seen = _make_recording_tool("list_models_for_agent")
    seen["untouched"] = True

    @asynccontextmanager
    async def fake_session(mcp_url: str, *, auth_header: str | None = None) -> AsyncIterator[_FakeSession]:
        """Yield the fake session in place of a Streamable-HTTP client.

        Args:
            mcp_url: Ignored.
            auth_header: Ignored.
        """
        yield _FakeSession(["list_models_for_agent", "submit_unified_run_optimize_post"])

    monkeypatch.setattr(generalist_module, "_mcp_session", fake_session)
    monkeypatch.setattr(dspy.Tool, "from_mcp_tool", staticmethod(lambda session, spec: tool))
    lm = _ScriptedLM([("list_models_for_agent", {"limit": None}), ("submit", {"assistant_message": "הנה המודלים"})])
    events: list[dict] = []

    reply = await generalist_module._drive_generalist_agent(
        mcp_url="http://unused/mcp/",
        wizard_state=WizardState(),
        chat_history=[
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "hello",
                "tools": [{"tool": "get_registry_snapshot", "status": "done", "result": "{}"}],
            },
        ],
        user_message="which models?",
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=events.append,
        lm=lm,
        reply_language="Hebrew",
    )

    assert reply == "הנה המודלים"
    assert seen == {"untouched": True}
    first = lm.requests[0]["messages"]
    assert [m["role"] for m in first] == ["system", "user", "assistant", "tool", "assistant", "tool", "user"]
    assert first[2]["tool_calls"][0]["function"]["name"] == "get_registry_snapshot"
    assert first[4]["tool_calls"][0]["function"]["name"] == "submit"
    assert "which models?" in first[-1]["content"]
    assert "hello" not in first[1]["content"]
    assert [t["function"]["name"] for t in lm.requests[0]["tools"]] == [
        "list_models_for_agent",
        "search_docs",
        "share_plan",
        "submit",
    ]
    # The second request only appends to the first: the cacheable prefix holds.
    assert lm.requests[1]["messages"][: len(first)] == first
    assert events[0]["event"] == "turn_metadata"
    assert events[0]["data"]["allowed_tools"] == ["list_models_for_agent", "search_docs", "share_plan"]
    assert [e["event"] for e in events if e["event"] in ("tool_start", "tool_end")] == ["tool_start", "tool_end"]
    # One clear per loop step, so a pending row whose call never ran is dropped.
    assert [e["event"] for e in events if e["event"] in ("tool_pending_clear", "tool_start")] == [
        "tool_pending_clear",
        "tool_start",
        "tool_pending_clear",
    ]
    assert not [e for e in events if e["event"] == "status_patch"]


class _ThinkingOnlyLM(_ScriptedLM):
    """An LM that spends every call thinking and never returns a tool call or text."""

    def forward(self, prompt=None, messages=None, **kwargs):
        """Record the request and return a reply cut off by the output cap.

        Args:
            prompt: Unused legacy prompt.
            messages: The rendered chat messages.
            **kwargs: Provider kwargs, including ``tools``.

        Returns:
            An OpenAI-shaped response with no content and no tool call.
        """
        self.requests.append({"messages": messages, **kwargs})
        return litellm.ModelResponse(
            model="scripted",
            choices=[{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": None}}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


@pytest.mark.asyncio
async def test_turn_that_never_answers_raises_instead_of_an_empty_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that only thinks fails the turn, so the chat shows an error rather than a blank message."""
    monkeypatch.setattr(settings, "firecrawl_api_key", None)

    @asynccontextmanager
    async def fake_session(mcp_url: str, *, auth_header: str | None = None) -> AsyncIterator[_FakeSession]:
        """Yield an empty fake session in place of a Streamable-HTTP client.

        Args:
            mcp_url: Ignored.
            auth_header: Ignored.
        """
        yield _FakeSession([])

    monkeypatch.setattr(generalist_module, "_mcp_session", fake_session)
    lm = _ThinkingOnlyLM([])

    with pytest.raises(AgentStoppedError, match="stopped before it answered"):
        await generalist_module._drive_generalist_agent(
            mcp_url="http://unused/mcp/",
            wizard_state=WizardState(),
            chat_history=[],
            user_message="build the code for me",
            trust_mode="yolo",
            registry=ApprovalRegistry(),
            emit=lambda event: None,
            lm=lm,
            reply_language="English",
        )
    assert lm.requests


@pytest.mark.asyncio
async def test_turn_offers_web_tools_when_firecrawl_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a Firecrawl key the turn lists the web tools and runs them through the gated wrapper."""
    monkeypatch.setattr(settings, "firecrawl_api_key", SecretStr("fc-test"))
    posted: list[tuple[str, dict]] = []

    async def fake_post(path: str, body: dict) -> dict:
        """Record the Firecrawl call and return one search hit.

        Args:
            path: Firecrawl endpoint path.
            body: Request body.

        Returns:
            A Firecrawl ``data`` payload.
        """
        posted.append((path, body))
        return {"web": [{"url": "https://a.example", "title": "A"}]}

    @asynccontextmanager
    async def fake_session(mcp_url: str, *, auth_header: str | None = None) -> AsyncIterator[_FakeSession]:
        """Yield an empty fake session in place of a Streamable-HTTP client.

        Args:
            mcp_url: Ignored.
            auth_header: Ignored.
        """
        yield _FakeSession([])

    monkeypatch.setattr(web_tools, "_firecrawl_post", fake_post)
    monkeypatch.setattr(generalist_module, "_mcp_session", fake_session)
    lm = _ScriptedLM([("web_search", {"query": "dspy gepa"}), ("submit", {"assistant_message": "found it"})])
    events: list[dict] = []

    reply = await generalist_module._drive_generalist_agent(
        mcp_url="http://unused/mcp/",
        wizard_state=WizardState(),
        chat_history=[],
        user_message="look up gepa",
        trust_mode="ask",
        registry=ApprovalRegistry(),
        emit=events.append,
        lm=lm,
        reply_language="English",
    )

    assert reply == "found it"
    assert events[0]["data"]["allowed_tools"] == sorted({*web_tools.WEB_TOOLS, "search_docs", "share_plan"})
    assert posted == [("/search", {"query": "dspy gepa", "limit": 5})]
    ends = [e["data"] for e in events if e["event"] == "tool_end"]
    assert [(e["tool"], e["status"]) for e in ends] == [("web_search", "ok")]


@pytest.mark.asyncio
async def test_turn_answers_from_the_docs_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The turn always offers ``search_docs`` and runs it through the gated wrapper."""
    (tmp_path / "en").mkdir()
    (tmp_path / "en" / "limits.md").write_text("## Spending limit\n\nEach run stops at its limit.\n", encoding="utf-8")
    monkeypatch.setattr(docs_tools, "_corpus", DocsCorpus(tmp_path, remote_url="http://unused.invalid/llms-full.txt"))

    @asynccontextmanager
    async def fake_session(mcp_url: str, *, auth_header: str | None = None) -> AsyncIterator[_FakeSession]:
        """Yield an empty fake session in place of a Streamable-HTTP client.

        Args:
            mcp_url: Ignored.
            auth_header: Ignored.
        """
        yield _FakeSession([])

    monkeypatch.setattr(generalist_module, "_mcp_session", fake_session)
    lm = _ScriptedLM(
        [
            ("search_docs", {"query": "spending limit", "locale": "en"}),
            ("submit", {"assistant_message": "See [Limits](/docs/limits)."}),
        ]
    )
    events: list[dict] = []

    reply = await generalist_module._drive_generalist_agent(
        mcp_url="http://unused/mcp/",
        wizard_state=WizardState(),
        chat_history=[],
        user_message="how do spending limits work?",
        trust_mode="ask",
        registry=ApprovalRegistry(),
        emit=events.append,
        lm=lm,
        reply_language="English",
    )

    assert reply == "See [Limits](/docs/limits)."
    assert "search_docs" in events[0]["data"]["allowed_tools"]
    ends = [e["data"] for e in events if e["event"] == "tool_end"]
    assert [(e["tool"], e["status"]) for e in ends] == [("search_docs", "ok")]
    assert "/docs/limits" in json.dumps(lm.requests[1]["messages"])


def test_share_plan_streams_a_plan_event_and_no_tool_row() -> None:
    """The plan reaches the panel as its own event, trimmed to one line."""
    events: list[dict] = []
    tool = plan_tool(events.append)

    assert tool.func(plan="   ").startswith("Empty plan")
    result = tool.func(plan="  I'll fill the inputs,\n then move on.  ")

    assert events == [{"event": "plan", "data": {"text": "I'll fill the inputs, then move on."}}]
    assert "Carry it out" in result


def test_share_plan_keeps_the_first_plan_of_a_turn() -> None:
    """A second plan in one turn is refused, so the shown plan never changes mid-turn."""
    events: list[dict] = []
    tool = plan_tool(events.append)

    tool.func(plan="Fill the inputs.")
    second = tool.func(plan="Actually, pick the optimizer.")

    assert [e["data"]["text"] for e in events] == ["Fill the inputs."]
    assert "already shown" in second


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Matching columns to inputs\nThe question column is the input. The answer is the output.",
            "Matching columns to inputs\nThe question column is the input.",
        ),
        (
            '**Checking the budget**\n\n"Five dollars covers a short run."',
            "Checking the budget\nFive dollars covers a short run.",
        ),
        ("**Reading the rows:** Each row is one ticket.", "Reading the rows\nEach row is one ticket."),
        ("- One. Two. Three.", "One."),
        ("Reading the rows", "Reading the rows"),
        (
            "This line is far too long to be a heading at all\nso it joins the sentence.",
            "This line is far too long to be a heading at all so it joins the sentence.",
        ),
        ("   ", None),
        ("x" * 300, "x" * 300),
    ],
)
def test_clean_summary_keeps_whole_sentences(raw: str, expected: str | None) -> None:
    """A label becomes a heading line and one whole sentence, unquoted and never cut mid-sentence."""
    assert _clean_summary(raw) == expected


@pytest.mark.parametrize(
    ("reasoning", "expected"),
    [
        (
            "The user wants support answers.  So pairs fit. Then the optimizer.",
            "The user wants support answers. So pairs fit.",
        ),
        ("", None),
        ("word " * 80, ("word " * 48).strip()),
        ("a, " * 100, ", ".join(["a"] * 80)),
    ],
)
def test_fallback_summary_keeps_the_opening_sentences(reasoning: str, expected: str | None) -> None:
    """A fallback label is the reasoning's opening sentences, cut only at a clause or word."""
    assert fallback_summary(reasoning) == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"reasoning_mandatory": True, "reasoning_efforts": ["none", "low", "high"]}, {"reasoning_effort": "low"}),
        ({"reasoning_efforts": ["none", "low"]}, {"reasoning_effort": "none"}),
        (
            {"reasoning_default_enabled": True, "reasoning_efforts": []},
            {"extra_body": {"reasoning": {"enabled": False}}},
        ),
        ({"reasoning_efforts": []}, {}),
    ],
)
def test_summary_model_answers_without_thinking(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, Any], expected: dict[str, Any]
) -> None:
    """The label model is asked not to think, at the weakest level a model that always thinks allows."""
    entry = SimpleNamespace(
        value="m", reasoning_mandatory=False, reasoning_default_enabled=False, reasoning_efforts=None
    )
    for key, value in row.items():
        setattr(entry, key, value)
    monkeypatch.setattr(generalist_module, "catalog_snapshot", lambda: SimpleNamespace(models=[entry]))

    assert generalist_module._no_thinking_extra("m") == expected
    assert generalist_module._no_thinking_extra("unknown") == {}


_LONG = "Looking at the task, the user wants support answers, so pairs fit best here."


async def test_thinking_summaries_label_each_row_the_panel_shows() -> None:
    """Stretches end where the panel's rows end, short ones are their own label, and indexes match."""
    published: list[dict] = []
    asked: list[str] = []

    async def summarize(text: str) -> str | None:
        """Return a canned label and record what was asked.

        Args:
            text: The stretch's reasoning.

        Returns:
            The label.
        """
        asked.append(text)
        return f"label {len(asked)}"

    summaries = ThinkingSummaries(summarize, published.append)
    for ev in [
        {"event": "reasoning_patch", "data": {"chunk": _LONG[:30]}},
        {"event": "reasoning_patch", "data": {"chunk": _LONG[30:]}},
        {"event": "tool_start", "data": {"id": "t1"}},
        {"event": "tool_start", "data": {"id": "t2"}},
        {"event": "reasoning_patch", "data": {"chunk": "Fine."}},
        {"event": "message_patch", "data": {"chunk": "Hi"}},
        {"event": "reasoning_patch", "data": {"chunk": _LONG}},
        {"event": "message_patch", "data": {"chunk": " there"}},
        {"event": "steer_applied", "data": {"ids": ["s1"], "text": "also this"}},
        {"event": "reasoning_patch", "data": {"chunk": _LONG}},
    ]:
        summaries.observe(ev)
    summaries.close()
    while await summaries.next_label():
        pass

    assert asked == [_LONG, _LONG, _LONG]
    assert sorted((e["data"]["index"], e["data"]["text"]) for e in published) == [
        (0, "label 1"),
        (1, "Fine."),
        (2, "label 2"),
        (3, "label 3"),
    ]


async def test_a_long_open_row_is_labelled_while_it_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """An open row is labelled on a timer, even when its reasoning went quiet, never after its closing label."""
    monkeypatch.setattr(generalist_module, "_LIVE_SUMMARY_FIRST_SECONDS", 0.02)
    monkeypatch.setattr(generalist_module, "_LIVE_SUMMARY_EVERY_SECONDS", 0.02)
    published: list[dict] = []
    asked: list[str] = []
    release = asyncio.Event()

    async def summarize(text: str) -> str | None:
        """Label the reasoning so far; the second call waits until released.

        Args:
            text: The stretch's reasoning.

        Returns:
            The label.
        """
        asked.append(text)
        if len(asked) == 2:
            await release.wait()
        return f"label {len(asked)}"

    summaries = ThinkingSummaries(summarize, published.append)
    summaries.observe({"event": "reasoning_patch", "data": {"chunk": _LONG}})
    await asyncio.sleep(0)
    assert asked == []

    await asyncio.sleep(0.05)
    assert asked == [_LONG]
    assert published == [{"event": "thinking_summary", "data": {"index": 0, "text": "label 1"}}]

    await asyncio.sleep(0.05)
    assert len(asked) == 1, "unchanged reasoning is not labelled twice"

    summaries.observe({"event": "reasoning_patch", "data": {"chunk": " more"}})
    await asyncio.sleep(0.05)
    assert len(asked) == 2
    summaries.observe({"event": "tool_start", "data": {"id": "t1"}})
    release.set()
    while await summaries.next_label():
        pass
    await asyncio.sleep(0.05)

    assert [e["data"]["text"] for e in published] == ["label 1", "label 3"]
    assert asked[1] == asked[2] == _LONG + " more"


async def test_failed_or_slow_summaries_fall_back_to_the_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A label that errors or misses the timeout is cut from the row's own reasoning, so no row is unlabeled."""
    monkeypatch.setattr(generalist_module, "_SUMMARY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(generalist_module, "_SUMMARY_HEDGE_SECONDS", 0.01)
    published: list[dict] = []

    async def summarize(text: str) -> str | None:
        """Fail on the first stretch and stall on the second.

        Args:
            text: The stretch's reasoning.

        Returns:
            Never returns in time.

        Raises:
            RuntimeError: On the first stretch.
        """
        if text.startswith("A"):
            raise RuntimeError("gateway down")
        await asyncio.sleep(1)
        return "too late"

    summaries = ThinkingSummaries(summarize, published.append)
    summaries.observe({"event": "reasoning_patch", "data": {"chunk": "A" + _LONG}})
    summaries.observe({"event": "tool_start", "data": {}})
    summaries.observe({"event": "reasoning_patch", "data": {"chunk": "B" + _LONG}})
    summaries.close()
    while await summaries.next_label():
        pass

    assert sorted((e["data"]["index"], e["data"]["text"]) for e in published) == [
        (0, fallback_summary("A" + _LONG)),
        (1, fallback_summary("B" + _LONG)),
    ]


async def test_a_slow_summary_is_hedged_and_the_first_label_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first call slower than the hedge delay starts a second one, whose label is published."""
    monkeypatch.setattr(generalist_module, "_SUMMARY_HEDGE_SECONDS", 0.01)
    published: list[dict] = []
    calls: list[int] = []

    async def summarize(text: str) -> str | None:
        """Stall on the first call, answer at once on the second.

        Args:
            text: The stretch's reasoning.

        Returns:
            The label.
        """
        calls.append(len(calls))
        if len(calls) == 1:
            await asyncio.sleep(5)
            return "too late"
        return "Your rows are pairs."

    summaries = ThinkingSummaries(summarize, published.append)
    summaries.observe({"event": "reasoning_patch", "data": {"chunk": _LONG}})
    summaries.close()
    while await summaries.next_label():
        pass

    assert len(calls) == 2
    assert [e["data"]["text"] for e in published] == ["Your rows are pairs."]


async def test_turn_meters_the_summary_model_once_per_long_stretch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every label reaches the stream, the last ones after ``done``, and the model is metered once."""

    class _LabelLM:
        """Stands in for the summary model."""

        calls = 0

        def __call__(self, messages: list[dict]) -> list[str]:
            """Return one label.

            Args:
                messages: The label prompt.

            Returns:
                The model's outputs.
            """
            _LabelLM.calls += 1
            assert "English" in messages[0]["content"]
            return ["Picked pairs for the inputs."]

    label_lm = _LabelLM()

    async def fake_drive(*, emit: Any, **_kwargs: Any) -> str:
        """Stream two long stretches around a tool call, then reply.

        Args:
            emit: The turn's emitter.

        Returns:
            The reply.
        """
        emit({"event": "reasoning_patch", "data": {"chunk": _LONG}})
        emit({"event": "tool_start", "data": {"id": "t1", "tool": "x"}})
        emit({"event": "reasoning_patch", "data": {"chunk": _LONG}})
        await asyncio.sleep(0)
        return "ok"

    monkeypatch.setattr(generalist_module, "_drive_generalist_agent", fake_drive)
    monkeypatch.setattr(generalist_module, "_build_generalist_lm", object)
    monkeypatch.setattr(generalist_module, "_build_summary_lm", lambda: label_lm)
    monkeypatch.setattr(generalist_module, "served_model_from", lambda lm: None)
    sink: list = []

    events = [
        ev
        async for ev in generalist_module.run_generalist_agent(
            wizard_state=WizardState(), chat_history=[], user_message="go", locale="en", usage_sink=sink
        )
    ]

    names = [e["event"] for e in events]
    assert names.count("done") == 1
    assert set(names[names.index("done") + 1 :]) <= {"thinking_summary"}
    labels = [e["data"] for e in events if e["event"] == "thinking_summary"]
    assert sorted(d["index"] for d in labels) == [0, 1]
    assert {d["text"] for d in labels} == {"Picked pairs for the inputs."}
    assert _LabelLM.calls == 2
    assert sink.count(label_lm) == 1


def test_thinking_trail_times_rows_from_the_previous_step() -> None:
    """A row after a tool starts when the tool ended, and only closed rows take a summary."""
    ticks = iter([1.0, 2.0, 2.0, 4.0, 5.0, 9.0])
    trail = ThinkingTrail(clock=lambda: next(ticks))

    trail.observe({"event": "reasoning_patch", "data": {"chunk": "first"}})
    assert trail.observe({"event": "tool_start", "data": {}}) == (0, "first")
    trail.observe({"event": "tool_end", "data": {}})
    trail.observe({"event": "thinking_summary", "data": {"index": 1, "text": "too early"}})
    trail.observe({"event": "reasoning_patch", "data": {"chunk": "second"}})
    trail.observe({"event": "thinking_summary", "data": {"index": 0, "text": "Read the task"}})

    assert trail.rows() == [
        {"startedAt": 1000, "toolsBefore": 0, "reasoning": "first", "endedAt": 2000, "summary": "Read the task"},
        {"startedAt": 4000, "toolsBefore": 1, "reasoning": "second", "endedAt": 9000},
    ]


def test_mcp_session_bounds_each_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool call that never answers times out instead of holding the turn open."""
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_transport(url: str, headers: dict[str, str] | None = None) -> AsyncIterator[tuple[Any, Any, Any]]:
        """Yield empty transport streams."""
        yield (None, None, None)

    class FakeSession:
        def __init__(self, read: Any, write: Any, **kwargs: Any) -> None:
            """Record the session kwargs."""
            seen.update(kwargs)

        async def __aenter__(self) -> FakeSession:
            """Return this session."""
            return self

        async def __aexit__(self, *exc: object) -> None:
            """Close nothing."""

        async def initialize(self) -> None:
            """Skip the handshake."""

    monkeypatch.setattr(generalist_module, "streamablehttp_client", fake_transport)
    monkeypatch.setattr(generalist_module, "ClientSession", FakeSession)

    async def run() -> None:
        """Open and close one MCP session."""
        async with generalist_module._mcp_session("http://mcp"):
            pass

    asyncio.run(run())
    assert seen["read_timeout_seconds"].total_seconds() == generalist_module.MCP_CALL_TIMEOUT_SECONDS


def test_every_folder_agent_mutation_has_an_approval_gate() -> None:
    """Each agent-tagged folder write is gated, so exposing it to the agent later cannot skip the confirm."""
    router = create_folders_router(job_store=None)
    writes = {
        route.operation_id
        for route in router.routes
        if isinstance(route, APIRoute) and "agent" in route.tags and route.methods - {"GET"}
    }
    assert writes == {"create_folder", "update_folder", "delete_folder", "move_folder", "move_runs_to_folder"}
    for name in writes:
        assert _needs_approval(name, "ask") is True
    # Each of these can change who sees a run: a run inherits its folder's access.
    for name in ("delete_folder", "move_folder", "move_runs_to_folder", "update_folder"):
        assert _needs_approval(name, "auto_safe") is True
    assert _needs_approval("create_folder", "auto_safe") is False


def _turn_guard(state: dict[str, Any], message: str = "hi") -> tuple[Any, _TurnAuthoringFlag, list[dict]]:
    """Build a screen turn's end guard with its flag and captured events.

    Args:
        state: The wizard snapshot.
        message: The message that started the turn.

    Returns:
        The guard (or ``None``), the turn flag and the emitted events.
    """
    turn = _TurnAuthoringFlag()
    events: list[dict] = []
    return screen_turn_guard(cast(WizardState, state), turn, message, events.append), turn, events


def test_turn_guard_refuses_a_statement_once_on_a_done_screen() -> None:
    """A done screen's statement-only reply is refused once, and the shown text is taken back."""
    guard, _, events = _turn_guard({**_READY, "wizard_screen": "brief"})
    refusal = guard({"assistant_message": "I saved the goal."})
    assert refusal
    assert "continue_to_next_screen" in refusal
    assert [event["event"] for event in events] == ["message_reset", "tool_start", "tool_end"]
    assert events[1]["data"]["tool"] == "turn_check"
    assert events[1]["data"]["arguments"] == {"reply": "I saved the goal."}
    assert events[2]["data"]["result"] == {"missing": [], "feedback": refusal}
    assert guard({"assistant_message": "I saved the goal."}) is None


def test_turn_guard_names_what_an_unfinished_screen_needs() -> None:
    """On an unfinished screen the refusal says what to ask for."""
    guard, _, _ = _turn_guard({"wizard_screen": "brief"})
    refusal = guard({"assistant_message": "Noted."})
    assert refusal
    assert "task_objective" in refusal


def test_turn_guard_accepts_a_question_or_a_move() -> None:
    """A question, in any script's question mark, or a screen move ends the turn."""
    guard, turn, _ = _turn_guard({**_READY, "wizard_screen": "brief"})
    assert guard({"assistant_message": "How much may it spend?"}) is None
    assert guard({"assistant_message": "\u0643\u0645\u061f"}) is None
    turn.screen_moved = True
    assert guard({"assistant_message": "Moved on."}) is None
    assert not turn.end_refused


@pytest.mark.asyncio
async def test_screen_moves_mark_the_turn() -> None:
    """Continue and Back set the flag the turn guard reads."""
    turn = _TurnAuthoringFlag()
    await _nav_tools({**_READY, "wizard_screen": "brief"}, turn)["continue_to_next_screen"].acall()
    assert turn.screen_moved
    back = _TurnAuthoringFlag()
    await _nav_tools({"wizard_screen": "budget"}, back)["go_back_to_screen"].acall(screen_id="brief", request="x")
    assert back.screen_moved


@pytest.mark.asyncio
async def test_ask_user_shows_one_question_with_choices() -> None:
    """A question with 2-4 options goes to the panel once a turn and lets the turn end."""
    turn = _TurnAuthoringFlag()
    ask = _nav_tools({"wizard_screen": "brief"}, turn)["ask_user"]
    options = '[{"label": "Exact match", "description": "Same text scores 1."}, "Judge model", {"label": " "}]'
    result = await ask.acall(question=" Which measure? ", options_json=options)
    assert result == {
        "question": "Which measure?",
        "options": [
            {"label": "Exact match", "description": "Same text scores 1."},
            {"label": "Judge model", "description": ""},
        ],
    }
    assert turn.asked
    assert "already shown" in await ask.acall(question="Again?", options_json=options)
    guard, guard_turn, _ = _turn_guard({**_READY, "wizard_screen": "brief"})
    guard_turn.asked = True
    assert guard({"assistant_message": "Pick a measure."}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("options_json", ["not json", "[]", '["Only one"]', '{"label": "x"}'])
async def test_ask_user_refuses_too_few_options(options_json: str) -> None:
    """Bad JSON or fewer than two options shows nothing and leaves the turn open."""
    turn = _TurnAuthoringFlag()
    result = await _nav_tools({"wizard_screen": "brief"}, turn)["ask_user"].acall(
        question="Which?", options_json=options_json
    )
    assert isinstance(result, str)
    assert not turn.asked


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({}, "hi"),
        ({"wizard_screen": "check"}, "hi"),
        ({"wizard_screen": "overview"}, "hi"),
        ({"wizard_screen": "brief", "cloned_run": True}, "hi"),
        ({"wizard_screen": "metric"}, f"{AUTOMATIC_TURN_PREFIX} failed"),
    ],
)
def test_turn_guard_is_off_where_a_statement_may_end_the_turn(state: dict[str, Any], message: str) -> None:
    """No wizard, the Check and Overview screens, cloned runs and automatic turns get no guard."""
    assert _turn_guard(state, message)[0] is None


def test_chat_replay_drops_turn_checks() -> None:
    """A turn check in a stored trace never replays as a tool the agent called."""
    history = _history_from_chat(
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "Which file?",
                "tools": [{"tool": "turn_check", "status": "done", "result": {"missing": []}}],
            },
        ]
    )
    names = [call.name for event in history.messages for call in getattr(event.get("tool_calls"), "tool_calls", [])]
    assert names == ["submit"]

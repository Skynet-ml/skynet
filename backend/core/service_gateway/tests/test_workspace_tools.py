"""Tests for the Metric screen's workspace tools: the setup agent reads the repository and writes the files."""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import dspy
import litellm
import pytest

from core.api.errors import DomainError
from core.api.routers.generalist_agent import _merge_wizard_patch
from core.config import settings
from core.connectors.github_repo import RepoRateLimitError
from core.models.blackbox import DRAFT_FILE_BYTES, DRAFT_FILES_TOTAL_BYTES
from core.service_gateway.agents import generalist as generalist_module
from core.service_gateway.agents import pi_react, repo_browser, workspace_tools
from core.service_gateway.agents.generalist import (
    REPO_COLUMN_MAPPING,
    SCREEN_FOCUS,
    VALIDATE_CODE_TOOL,
    ApprovalRegistry,
    WizardState,
    _screen_problems,
    _TurnAuthoringFlag,
    _wrap_tool_with_approval,
    screen_nav_tools,
    screen_tools,
    tools_for,
)
from core.service_gateway.agents.workspace_tools import (
    ASCENT_TOML_EXAMPLE,
    EDIT_FILE_TOOL,
    LIST_REPO_TOOL,
    METRIC_GUIDE,
    READ_REPO_TOOL,
    WRITE_FILE_TOOL,
    open_repo,
)
from core.service_gateway.agents.workspace_tools import (
    workspace_tools as build_workspace_tools,
)
from core.service_gateway.optimization.blackbox import repo_constraints

from .test_pi_react import _simulated_pi


@pytest.fixture(autouse=True)
def _simulated_pi_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the agent turns on the in-process Pi stand-in, so no ``pi`` binary is needed.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
    """
    monkeypatch.setattr(pi_react, "run_pi_turn", _simulated_pi)


_REPO_METRIC: dict[str, Any] = {
    "wizard_screen": "metric",
    "job_name": "Faster tests",
    "task_objective": "Make the test suite pass faster",
    "repository": "acme/shop",
    "github_connected": True,
}
_SCORER = "def score(repo_path):\n    return {'score': 1.0, 'feedback': 'ok'}\n"
# The metric scores only what a [run] or a target stage gives it, so a valid contract names one (SKY-761).
_VALID_TOML = 'version = 1\n[run]\nentry = "run.py:main"\n'
# The commit every branch resolves to in these tests, unless a test says otherwise.
_HEAD = "a" * 40


@pytest.fixture(autouse=True)
def pinned_head(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Resolve every ref to ``_HEAD`` without a host call.

    Args:
        monkeypatch: The test's monkeypatch.

    Returns:
        The refs the workspace tools asked to resolve.
    """
    asked: list[str | None] = []

    def resolve_commit(provider: str | None, token: str | None, repository: str, branch: str | None) -> str:
        """Record the ref and return the fixed commit."""
        asked.append(branch)
        return _HEAD

    monkeypatch.setattr(workspace_tools.repo_hosts, "resolve_commit", resolve_commit)
    return asked


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


def _fake_github(monkeypatch: pytest.MonkeyPatch, reads: list[str], token_seen: str | None = "gh-token") -> None:
    """Stand in for GitHub at the ``github`` module boundary.

    Args:
        monkeypatch: The test's monkeypatch.
        reads: Collects every file path read.
        token_seen: The token every call must carry; ``None`` for an anonymous read.
    """

    def tree_entries(token: str | None, full_name: str, branch: str) -> dict[str, Any]:
        """List a small repository."""
        assert token == token_seen
        assert full_name == "acme/shop"
        paths = ["README.md", "pyproject.toml", "tests/test_app.py"]
        return {"entries": [{"path": p, "type": "file", "size": 10} for p in paths], "truncated": False}

    def read_text_file(token: str | None, full_name: str, branch: str, path: str, max_bytes: int) -> tuple[str, bool]:
        """Return the file's name as its text."""
        assert token == token_seen
        reads.append(path)
        return f"contents of {path}", False

    monkeypatch.setattr(workspace_tools.github, "tree_entries", tree_entries)
    monkeypatch.setattr(repo_browser.github, "read_text_file", read_text_file)


def test_repo_metric_screen_offers_validate_code_without_a_dataset() -> None:
    """A repository run has no dataset, yet its Metric screen keeps the code check."""
    assert VALIDATE_CODE_TOOL in screen_tools(tools_for(WizardState(**_REPO_METRIC)), WizardState(**_REPO_METRIC))


@pytest.mark.asyncio
async def test_validate_code_gets_a_column_mapping_on_a_repository_run() -> None:
    """The code check needs a mapped input, so the wrapper fills one for a repository scorer."""
    seen: dict[str, Any] = {}

    async def validate(**kwargs: Any) -> dict[str, Any]:
        """Record the call's arguments."""
        seen.update(kwargs)
        return {"valid": True}

    tool = dspy.Tool(validate, name=VALIDATE_CODE_TOOL, desc="check", args={}, arg_types={}, arg_desc={})
    wrapped = _wrap_tool_with_approval(
        tool,
        trust_mode="yolo",
        registry=ApprovalRegistry(),
        emit=lambda _e: None,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=WizardState(**_REPO_METRIC),
    )
    await wrapped.func._async_body(metric_code=_SCORER, column_mapping={})
    assert seen == {"metric_code": _SCORER, "column_mapping": REPO_COLUMN_MAPPING}


@pytest.mark.asyncio
async def test_a_failed_workspace_read_ends_as_an_error_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing repository file ends with status "error" and its text; an API tool's "error" field does not."""
    _fake_github(monkeypatch, [])
    events: list[dict[str, Any]] = []

    def wrap(tool: dspy.Tool) -> Any:
        """Gate one tool the way a screen turn does, and record its events."""
        return _wrap_tool_with_approval(
            tool,
            trust_mode="yolo",
            registry=ApprovalRegistry(),
            emit=events.append,
            outer_loop=asyncio.get_running_loop(),
            wizard_state=WizardState(**_REPO_METRIC),
        )

    read = wrap(_tools(_REPO_METRIC, _TurnAuthoringFlag(), lambda: "gh-token")[READ_REPO_TOOL])
    await read.func._async_body(path="missing.py")
    await read.func._async_body(path="README.md")

    async def api_call() -> dict[str, Any]:
        """Answer like an API whose data has an "error" field."""
        return {"error": "a field, not a failure"}

    await wrap(
        dspy.Tool(api_call, name="get_run_errors", desc="x", args={}, arg_types={}, arg_desc={})
    ).func._async_body()

    ends = [event["data"] for event in events if event["event"] == "tool_end"]
    assert ends[0]["status"] == "error"
    assert "No file named 'missing.py'" in ends[0]["result"]["error"]
    assert [end["status"] for end in ends[1:]] == ["ok", "ok"]


@pytest.mark.asyncio
async def test_a_failed_workspace_write_or_edit_ends_as_an_error_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused write or edit ends with status "error"; an ascent.toml parser error alone stays "ok".

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
    """
    _fake_github(monkeypatch, [])
    events: list[dict[str, Any]] = []

    def wrap(tool: dspy.Tool) -> Any:
        """Gate one tool the way a screen turn does, and record its events."""
        return _wrap_tool_with_approval(
            tool,
            trust_mode="yolo",
            registry=ApprovalRegistry(),
            emit=events.append,
            outer_loop=asyncio.get_running_loop(),
            wizard_state=WizardState(**_REPO_METRIC, metric_code=_SCORER),
        )

    tools = _tools({**_REPO_METRIC, "metric_code": _SCORER}, _TurnAuthoringFlag(), token=lambda: "gh-token")
    # The gate replaces the tool's func in place, so each tool is wrapped once.
    write = wrap(tools[WRITE_FILE_TOOL])
    await write.func._async_body(path="run.py", text=_SCORER)
    await wrap(tools[EDIT_FILE_TOOL]).func._async_body(path="metric.py", old_text="nothing", new_text="x")
    await write.func._async_body(path="ascent.toml", text="version = 1\n[run]\ntimeout = 30\n")

    ends = [event["data"] for event in events if event["event"] == "tool_end"]
    assert [end["status"] for end in ends] == ["error", "error", "ok"]
    assert ends[0]["result"]["error"].startswith("run.py cannot be written")
    assert "error" in ends[1]["result"]
    assert "ascent_toml_diagnostics" in ends[2]["result"]
    assert "error" not in ends[2]["result"]


def test_repo_metric_screen_needs_only_the_scorer() -> None:
    """A repository run has no run.py, so the scorer alone completes the Metric screen."""
    assert _screen_problems("metric", WizardState(**_REPO_METRIC)) == ["metric_code must be written"]
    assert _screen_problems("metric", WizardState(**_REPO_METRIC, metric_code=_SCORER)) == []


def test_an_invalid_ascent_toml_holds_the_metric_screen() -> None:
    """The Metric screen is not done while the draft's ascent.toml fails the run's parser."""
    bad = WizardState(**_REPO_METRIC, metric_code=_SCORER, ascent_toml="version = 2")
    assert _screen_problems("metric", bad) == [
        "ascent.toml is not valid: ascent.toml: version 2 is not supported; use version = 1. (line 1)",
        (
            "ascent.toml is not valid: ascent.toml: the metric cannot score a version with no output. Add a [run] "
            "section or a [target.<name>] stage whose outputs the metric scores."
        ),
    ]
    assert _screen_problems("metric", WizardState(**_REPO_METRIC, metric_code=_SCORER, ascent_toml=_VALID_TOML)) == []
    assert _screen_problems("metric", WizardState(**_REPO_METRIC, metric_code=_SCORER, ascent_toml="  ")) == []


def test_a_wizard_state_with_the_retired_field_has_no_draft_contract() -> None:
    """A wizard snapshot stored before SKY-746 keeps its text under the old field; the agent reads no draft contract."""
    retired = "_".join(("optimize", "toml"))  # Built from parts so the tree holds no retired name.
    state = cast(WizardState, {**_REPO_METRIC, "metric_code": _SCORER, retired: "version = 2"})

    assert _screen_problems("metric", state) == []
    assert workspace_tools.has_drafts(dict(state)) is False


async def test_check_setup_reports_the_parser_error_and_holds_continue() -> None:
    """``check_setup`` returns every error, and Continue refuses with them until a fix lands this turn."""
    turn = _TurnAuthoringFlag()
    state = WizardState(**{**_REPO_METRIC, "wizard_screen": "metric"}, metric_code=_SCORER, ascent_toml="x = [")
    tools = {tool.name: tool for tool in screen_nav_tools(state, turn)}

    result = await tools["check_setup"].acall()
    assert result["ok"] is False
    assert result["errors"][0]["message"].startswith("ascent.toml is not valid TOML")
    refused = await tools["continue_to_next_screen"].acall()
    assert isinstance(refused, str)
    assert "ascent.toml is not valid" in refused

    turn.wizard_patch["ascent_toml"] = _VALID_TOML
    assert await tools["check_setup"].acall() == {"ok": True, "errors": []}
    assert (await tools["continue_to_next_screen"].acall())["screen_nav"]["action"] == "continue"


def test_check_setup_is_offered_only_on_the_metric_screen() -> None:
    """Only the Metric screen, where ascent.toml is written, gets ``check_setup``."""
    names = [tool.name for tool in screen_nav_tools(WizardState(wizard_screen="brief"), _TurnAuthoringFlag())]
    assert "check_setup" not in names


def _tools(state: dict[str, Any], turn: _TurnAuthoringFlag, token: Any = None) -> dict[str, dspy.Tool]:
    """Build the workspace tools for one state, by name.

    Args:
        state: The wizard state.
        turn: The turn's shared flag.
        token: The caller's token function.

    Returns:
        The tools by name.
    """
    return {tool.name: tool for tool in build_workspace_tools(WizardState(**state), turn, github_token=token)}


def test_metric_focus_has_the_agent_read_and_write_the_code_itself() -> None:
    """The Metric instructions name the workspace tools and the guide, never a hand-off or fetch_url."""
    focus = SCREEN_FOCUS["metric"]
    for name in (LIST_REPO_TOOL, READ_REPO_TOOL, WRITE_FILE_TOOL, EDIT_FILE_TOOL):
        assert name in focus
    assert METRIC_GUIDE in focus
    assert "never ``fetch_url``" in focus
    assert "hand_off_to_coding_agent" not in focus


def test_the_coding_agent_hand_off_is_gone() -> None:
    """No screen offers the old hand-off tool, and its module no longer exists."""
    turn = _TurnAuthoringFlag()
    names = [*_tools(_REPO_METRIC, turn), *(t.name for t in screen_nav_tools(WizardState(**_REPO_METRIC), turn))]
    assert "hand_off_to_coding_agent" not in names
    assert not hasattr(generalist_module, "coding_agent_tools")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("core.service_gateway.agents.workspace_coder")


def test_workspace_tools_are_offered_on_the_metric_and_check_screens_only() -> None:
    """The four tools exist on the Metric screen and on the Check screen, which fixes metric.py, and nowhere else."""
    turn = _TurnAuthoringFlag()
    tools = [LIST_REPO_TOOL, READ_REPO_TOOL, WRITE_FILE_TOOL, EDIT_FILE_TOOL]
    assert list(_tools(_REPO_METRIC, turn)) == tools
    assert list(_tools({**_REPO_METRIC, "wizard_screen": "check"}, turn)) == tools
    assert _tools({**_REPO_METRIC, "wizard_screen": "budget"}, turn) == {}


def test_open_repo_reads_the_run_ref_not_the_default_branch(
    monkeypatch: pytest.MonkeyPatch, pinned_head: list[str | None]
) -> None:
    """The agent's drafts are whole-file copies, so it lists and reads the run's branch, pinned to one commit."""
    refs: list[str] = []

    def tree_entries(token: str | None, full_name: str, branch: str) -> dict[str, Any]:
        """Record the ref and list one file."""
        refs.append(branch)
        return {"entries": [{"path": "app.py", "type": "file", "size": 1}], "truncated": False}

    def read_text_file(token: str | None, full_name: str, branch: str, path: str, max_bytes: int) -> tuple[str, bool]:
        """Record the ref and return a fixed text."""
        refs.append(branch)
        return "x = 1\n", False

    monkeypatch.setattr(workspace_tools.github, "tree_entries", tree_entries)
    monkeypatch.setattr(repo_browser.github, "read_text_file", read_text_file)

    browser, _ = open_repo({**_REPO_METRIC, "repo_branch": " feature/x "}, lambda: "gh-token")
    assert browser is not None
    browser.read_repo_file("app.py")

    assert pinned_head == ["feature/x"]
    assert set(refs) == {_HEAD}
    assert browser.branch == _HEAD


def test_open_repo_reads_a_picked_commit_without_a_lookup(
    monkeypatch: pytest.MonkeyPatch, pinned_head: list[str | None]
) -> None:
    """A run on a picked commit reads that commit; no branch lookup runs."""
    _fake_github(monkeypatch, [])
    picked = "b" * 40

    browser, _ = open_repo({**_REPO_METRIC, "repo_branch": picked}, lambda: "gh-token")

    assert browser is not None
    assert browser.branch == picked
    assert pinned_head == []


def test_open_repo_keeps_reading_the_base_of_the_drafts(
    monkeypatch: pytest.MonkeyPatch, pinned_head: list[str | None]
) -> None:
    """While drafts exist, the agent reads the commit they were written on, so its next edit matches them."""
    _fake_github(monkeypatch, [])
    base = "c" * 40
    drafted = {**_REPO_METRIC, "repo_branch": "main", "workspace_base": base}

    with_drafts, _ = open_repo({**drafted, "workspace_files": {"app.py": "x = 2\n"}}, lambda: "gh-token")
    with_toml, _ = open_repo({**drafted, "ascent_toml": "version = 1\n"}, lambda: "gh-token")
    without_drafts, _ = open_repo(drafted, lambda: "gh-token")

    assert with_drafts is not None
    assert with_drafts.branch == base
    assert with_toml is not None
    assert with_toml.branch == base
    assert without_drafts is not None
    assert without_drafts.branch == _HEAD
    assert pinned_head == ["main"]


def test_open_repo_reports_a_rate_limit_on_the_commit_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spent rate limit on the branch lookup gives the rate-limit text, not a failed turn."""

    def limited(provider: str | None, token: str | None, repository: str, branch: str | None) -> str:
        """Fail like GitHub after the quota is spent."""
        raise RepoRateLimitError("GitHub's rate limit is reached. Try again later.")

    monkeypatch.setattr(workspace_tools.repo_hosts, "resolve_commit", limited)

    browser, reason = open_repo(_REPO_METRIC, lambda: "gh-token")

    assert browser is None
    assert reason == "GitHub's rate limit is reached. Try again later."


def test_open_repo_reads_a_github_repository_but_not_a_gitlab_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GitHub repository opens with the caller's token; a GitLab repository does not open."""
    _fake_github(monkeypatch, [])

    def token() -> str:
        """Return the caller's token."""
        return "gh-token"

    gitlab_run = {**_REPO_METRIC, "repo_provider": "gitlab"}
    for token_fn in (None, lambda: None):
        browser, reason = open_repo(gitlab_run, token_fn)
        assert browser is None
        assert reason

    browser, reason = open_repo(_REPO_METRIC, token)
    assert browser is not None
    assert reason == ""


def test_open_repo_reads_a_public_github_repository_without_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a GitHub link, the repository opens and reads anonymously (SKY-568)."""
    reads: list[str] = []
    _fake_github(monkeypatch, reads, token_seen=None)
    unlinked = {**_REPO_METRIC, "github_connected": False}

    for token_fn in (None, lambda: None, lambda: ""):
        browser, reason = open_repo(unlinked, token_fn)
        assert browser is not None
        assert reason == ""
    assert "README.md" in reads


def test_open_repo_says_only_a_public_repository_reads_without_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """An anonymous listing that GitHub refuses explains that only a public repository can be read."""

    def missing(token: str | None, full_name: str, branch: str) -> dict[str, Any]:
        """Answer like GitHub for a private or missing repository."""
        raise DomainError("connectors.not_found", status=404, provider="GitHub")

    monkeypatch.setattr(workspace_tools.github, "tree_entries", missing)

    browser, reason = open_repo({**_REPO_METRIC, "github_connected": False}, None)
    linked_browser, linked_reason = open_repo(_REPO_METRIC, lambda: "gh-token")

    assert browser is None
    assert "only a public repository can be read" in reason
    assert "Connect GitHub to optimize a private repository." in reason
    assert linked_browser is None
    assert linked_reason == "GitHub refused to list acme/shop."


def test_open_repo_reads_anonymously_when_the_token_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vault failure while loading the token falls back to an anonymous read, not a failed turn."""
    _fake_github(monkeypatch, [], token_seen=None)

    def broken_token() -> str:
        """Fail like a database error inside the vault."""
        raise RuntimeError("database unavailable")

    browser, reason = open_repo(_REPO_METRIC, broken_token)

    assert browser is not None
    assert reason == ""


def test_open_repo_reports_the_rate_limit_not_a_private_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spent rate limit names the limit instead of the public-only advice; only an unlinked read advises to connect."""

    def limited(token: str | None, full_name: str, branch: str) -> dict[str, Any]:
        """Answer like GitHub after the quota is spent."""
        raise DomainError("connectors.rate_limited", status=429, provider="GitHub")

    monkeypatch.setattr(workspace_tools.github, "tree_entries", limited)

    browser, reason = open_repo({**_REPO_METRIC, "github_connected": False}, None)
    linked_browser, linked_reason = open_repo(_REPO_METRIC, lambda: "gh-token")

    assert browser is None
    assert reason == "GitHub's rate limit is reached. Try again later. Connect GitHub to raise the limit."
    assert linked_browser is None
    assert linked_reason == "GitHub's rate limit is reached. Try again later."


@pytest.mark.asyncio
async def test_read_tools_open_the_repository_once_and_show_the_root_overview(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first read opens the repository; the root listing returns the tree with the README and manifest."""
    reads: list[str] = []
    opens: list[str] = []
    _fake_github(monkeypatch, reads)
    real_open = workspace_tools.open_repo

    def counting_open(state: dict[str, Any], token: Any) -> Any:
        """Count each open."""
        opens.append(state["repository"])
        return real_open(state, token)

    monkeypatch.setattr(workspace_tools, "open_repo", counting_open)
    tools = _tools(_REPO_METRIC, _TurnAuthoringFlag(), lambda: "gh-token")
    assert opens == []

    overview = await tools[LIST_REPO_TOOL].acall(path="")
    assert "Repository acme/shop" in overview
    assert "contents of README.md" in overview
    assert "contents of pyproject.toml" in overview
    folder = await tools[LIST_REPO_TOOL].acall(path="tests")
    assert "test_app.py" in folder
    text = await tools[READ_REPO_TOOL].acall(path="tests/test_app.py")
    assert "contents of tests/test_app.py" in text
    missing = await tools[READ_REPO_TOOL].acall(path="../secrets.txt")
    assert missing["error"].startswith("No file named '../secrets.txt'")

    assert opens == ["acme/shop"]
    assert reads == ["README.md", "pyproject.toml", "tests/test_app.py"]


@pytest.mark.asyncio
async def test_read_tools_keep_the_read_limit_of_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the browser's reads for one turn, a read returns the limit as an error and reaches no host."""
    reads: list[str] = []
    _fake_github(monkeypatch, reads)
    tools = _tools(_REPO_METRIC, _TurnAuthoringFlag(), lambda: "gh-token")

    for _ in range(repo_browser.READS_PER_TURN):
        await tools[READ_REPO_TOOL].acall(path="tests/test_app.py")
    over = await tools[READ_REPO_TOOL].acall(path="tests/test_app.py")

    assert over["error"].startswith("Read limit for this turn reached.")
    assert reads.count("tests/test_app.py") == repo_browser.READS_PER_TURN


@pytest.mark.asyncio
async def test_folder_listing_keeps_the_read_limit_of_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the turn's reads, a root listing returns the limit as an error and reaches no host."""
    reads: list[str] = []
    _fake_github(monkeypatch, reads)
    tools = _tools(_REPO_METRIC, _TurnAuthoringFlag(), lambda: "gh-token")

    for _ in range(repo_browser.READS_PER_TURN):
        await tools[READ_REPO_TOOL].acall(path="tests/test_app.py")
    before = list(reads)
    over = await tools[LIST_REPO_TOOL].acall(path="")

    assert over["error"].startswith("Read limit for this turn reached.")
    assert reads == before


@pytest.mark.asyncio
async def test_read_tools_return_why_the_repository_cannot_be_read() -> None:
    """A GitLab run with no link gets the reason as the tool's error result, not a failed turn."""
    tools = _tools({**_REPO_METRIC, "repo_provider": "gitlab"}, _TurnAuthoringFlag())

    assert await tools[READ_REPO_TOOL].acall(path="README.md") == {
        "error": "GitLab is not linked, so the repository cannot be read."
    }


@pytest.mark.asyncio
async def test_write_puts_the_file_into_the_turn_and_the_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A write lands in the turn's patch and comes back as a wizard_state patch for the editor.

    The first ascent.toml write reads the repository for a current file, so it records that commit.
    """
    _fake_github(monkeypatch, [])
    turn = _TurnAuthoringFlag()
    tools = _tools(_REPO_METRIC, turn, token=lambda: "gh-token")

    result = await tools[WRITE_FILE_TOOL].acall(path="metric.py", text=_SCORER)
    toml_result = await tools[WRITE_FILE_TOOL].acall(path="/ascent.toml", text=ASCENT_TOML_EXAMPLE)

    assert result == {"wizard_state": {"metric_code": _SCORER}}
    assert toml_result == {"wizard_state": {"workspace_base": _HEAD, "ascent_toml": ASCENT_TOML_EXAMPLE}}
    assert turn.wizard_patch == {
        "metric_code": _SCORER,
        "workspace_base": _HEAD,
        "ascent_toml": ASCENT_TOML_EXAMPLE,
    }


@pytest.mark.asyncio
async def test_write_reports_the_parser_error_of_ascent_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file the parser refuses still reaches the draft, with the error and its place for the agent."""
    _fake_github(monkeypatch, [])
    turn = _TurnAuthoringFlag()
    toml_text = "version = 1\n[run]\ntimeout = 30\n"

    result = await _tools(_REPO_METRIC, turn, token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=toml_text
    )

    assert result["wizard_state"] == {"workspace_base": _HEAD, "ascent_toml": toml_text}
    [error] = result["ascent_toml_diagnostics"]
    assert (error["level"], error["line"], error["section"], error["key"]) == ("error", 2, "run", "entry")
    assert "[run] entry is missing" in error["message"]
    assert turn.wizard_patch["ascent_toml"] == toml_text


@pytest.mark.asyncio
async def test_write_reports_every_mistake_of_ascent_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file with three mistakes gives three errors in one result, so the agent fixes all in one turn (SKY-763)."""
    _fake_github(monkeypatch, [])
    toml_text = 'version = 1\n[run]\nentry = "a.py:f"\nbogus = 1\n[metric]\nentry = 5\n[paths]\neditable = 3\n'

    result = await _tools(_REPO_METRIC, _TurnAuthoringFlag(), token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=toml_text
    )

    assert [(e["level"], e["line"], e["section"], e["key"]) for e in result["ascent_toml_diagnostics"]] == [
        ("error", 4, "run", "bogus"),
        ("error", 8, "paths", "editable"),
        ("error", 6, "metric", "entry"),
    ]


def _many_diagnostics(errors: int, warnings: int) -> list[dict[str, Any]]:
    """Build a made-up parser result: the warnings first, then the errors.

    Args:
        errors: How many errors.
        warnings: How many warnings.

    Returns:
        The diagnostics, in the shape of ``ascent_toml_diagnostics``.
    """
    return [
        *({"level": "warning", "line": i, "section": "", "key": "", "message": f"w{i}"} for i in range(warnings)),
        *({"level": "error", "line": i, "section": "", "key": "", "message": f"e{i}"} for i in range(errors)),
    ]


@pytest.mark.asyncio
async def test_write_caps_the_diagnostics_the_model_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """One bad write gives the model at most 20 diagnostics, errors first, and the count of the rest (SKY-763)."""
    _fake_github(monkeypatch, [])
    monkeypatch.setattr(workspace_tools, "ascent_toml_diagnostics", lambda _text: (_many_diagnostics(5, 25), 0))

    result = await _tools(_REPO_METRIC, _TurnAuthoringFlag(), token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text="version = 1\n"
    )

    kept = result["ascent_toml_diagnostics"]
    assert [e["message"] for e in kept] == [*(f"e{i}" for i in range(5)), *(f"w{i}" for i in range(15))]
    assert result["ascent_toml_diagnostics_omitted"] == 10


@pytest.mark.asyncio
async def test_a_short_diagnostic_list_has_no_omitted_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty diagnostics or fewer reach the model whole, with no omitted count."""
    _fake_github(monkeypatch, [])
    monkeypatch.setattr(workspace_tools, "ascent_toml_diagnostics", lambda _text: (_many_diagnostics(20, 0), 0))

    result = await _tools(_REPO_METRIC, _TurnAuthoringFlag(), token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text="version = 1\n"
    )

    assert len(result["ascent_toml_diagnostics"]) == 20
    assert "ascent_toml_diagnostics_omitted" not in result


async def test_check_setup_and_continue_cap_the_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    """``check_setup`` and the Continue refusal give at most 20 diagnostics and count the rest (SKY-763)."""
    monkeypatch.setattr(generalist_module, "ascent_toml_diagnostics", lambda _text: (_many_diagnostics(30, 3), 0))
    turn = _TurnAuthoringFlag()
    state = WizardState(**{**_REPO_METRIC, "wizard_screen": "metric"}, metric_code=_SCORER, ascent_toml="x = [")
    tools = {tool.name: tool for tool in screen_nav_tools(state, turn)}

    result = await tools["check_setup"].acall()
    assert result["ok"] is False
    assert [e["message"] for e in result["errors"]] == [f"e{i}" for i in range(20)]
    assert result["errors_omitted"] == 13
    refused = await tools["continue_to_next_screen"].acall()
    assert "e19 (line 19)" in refused
    assert "e20 (line 20)" not in refused
    assert "ascent.toml has 10 more errors; call check_setup after the fix." in refused


# 600 unsupported keys: the parser lists 500 errors, adds one summary item and hides 100 (SKY-763).
_OVER_THE_CAP = 'version = 1\n[paths]\neditable = ["src"]\n[run]\nentry = "src/run.py:main"\n' + "".join(
    f"k{i} = 1\n" for i in range(600)
)


@pytest.mark.asyncio
async def test_a_file_past_the_parser_cap_gives_the_true_omitted_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parser's summary item is dropped and its hidden errors count, in every model-facing result (SKY-763)."""
    _fake_github(monkeypatch, [])
    diagnostics, hidden = workspace_tools.ascent_toml_diagnostics(_OVER_THE_CAP)
    assert (len(diagnostics), hidden) == (500, 100)
    assert not any("not shown" in item["message"] for item in diagnostics)

    result = await _tools(_REPO_METRIC, _TurnAuthoringFlag(), token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=_OVER_THE_CAP
    )
    assert len(result["ascent_toml_diagnostics"]) == 20
    assert result["ascent_toml_diagnostics_omitted"] == 580

    state = WizardState(**{**_REPO_METRIC, "wizard_screen": "metric"}, metric_code=_SCORER, ascent_toml=_OVER_THE_CAP)
    tools = {tool.name: tool for tool in screen_nav_tools(state, _TurnAuthoringFlag())}
    assert (await tools["check_setup"].acall())["errors_omitted"] == 580
    refused = await tools["continue_to_next_screen"].acall()
    assert "ascent.toml has 580 more errors; call check_setup after the fix." in refused


async def test_the_continue_refusal_says_one_more_error_in_the_singular(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty-one errors give twenty lines and "1 more error", not "1 more errors"."""
    monkeypatch.setattr(generalist_module, "ascent_toml_diagnostics", lambda _text: (_many_diagnostics(21, 0), 0))
    state = WizardState(**{**_REPO_METRIC, "wizard_screen": "metric"}, metric_code=_SCORER, ascent_toml="x = [")
    tools = {tool.name: tool for tool in screen_nav_tools(state, _TurnAuthoringFlag())}

    refused = await tools["continue_to_next_screen"].acall()

    assert "ascent.toml has 1 more error; call check_setup after the fix." in refused


def test_omitted_text_uses_the_singular_for_one() -> None:
    """The MCP count line reads right for one and for many."""
    assert workspace_tools.omitted_text(1).startswith("ascent.toml: 1 more diagnostic is not shown.")
    assert workspace_tools.omitted_text(2).startswith("ascent.toml: 2 more diagnostics are not shown.")


def test_diagnostic_text_keeps_the_message_and_adds_the_line() -> None:
    """A plain-text diagnostic is the parser's message, then its line; a warning says so first."""
    error = {"level": "error", "line": None, "section": None, "key": "version", "message": "ascent.toml: bad."}
    assert workspace_tools.diagnostic_text(error) == "ascent.toml: bad."
    assert workspace_tools.diagnostic_text({**error, "line": 3}) == "ascent.toml: bad. (line 3)"
    assert workspace_tools.diagnostic_text({**error, "level": "warning", "line": 3}) == (
        "Warning: ascent.toml: bad. (line 3)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "text", "refusal"),
    [
        ("run.py", _SCORER, "run.py cannot be written"),
        ("../outside.py", _SCORER, "must stay inside the repository"),
        ("src\\app.py", _SCORER, "character that is not allowed"),
        (".git/config", _SCORER, ".git folder cannot be written"),
        ("src/.GIT/hooks/x", _SCORER, ".git folder cannot be written"),
        ("metric.py", " ", "Empty"),
        ("src/app.py", "", "Empty"),
    ],
)
async def test_write_refuses_unsafe_paths_run_py_and_empty_text(path: str, text: str, refusal: str) -> None:
    """A path outside the repository, inside .git, run.py or an empty text is refused, and nothing reaches the turn."""
    turn = _TurnAuthoringFlag()

    result = await _tools(_REPO_METRIC, turn)[WRITE_FILE_TOOL].acall(path=path, text=text)

    assert list(result) == ["error"]
    assert refusal in result["error"]
    assert turn.wizard_patch == {}


@pytest.mark.asyncio
async def test_edit_replaces_one_exact_part_of_the_current_file() -> None:
    """An edit starts from the user's file, then from this turn's writes; each refusal is an error and changes nothing."""
    turn = _TurnAuthoringFlag()
    tools = _tools({**_REPO_METRIC, "metric_code": _SCORER}, turn)

    first = await tools[EDIT_FILE_TOOL].acall(path="metric.py", old_text="1.0", new_text="0.5")
    second = await tools[EDIT_FILE_TOOL].acall(path="metric.py", old_text="'ok'", new_text="'half'")
    missing = await tools[EDIT_FILE_TOOL].acall(path="metric.py", old_text="nothing", new_text="x")
    repeated = await tools[EDIT_FILE_TOOL].acall(path="metric.py", old_text="r", new_text="x")
    other = await tools[EDIT_FILE_TOOL].acall(path="run.py", old_text="a", new_text="b")
    no_old = await tools[EDIT_FILE_TOOL].acall(path="metric.py", old_text="", new_text="x")
    edited = _SCORER.replace("1.0", "0.5").replace("'ok'", "'half'")
    blank = await tools[EDIT_FILE_TOOL].acall(path="metric.py", old_text=edited, new_text=" ")

    assert first == {"wizard_state": {"metric_code": _SCORER.replace("1.0", "0.5")}}
    assert second == {"wizard_state": {"metric_code": edited}}
    assert "is not in metric.py" in missing["error"]
    assert "times in metric.py" in repeated["error"]
    assert other["error"].startswith("run.py cannot be written")
    assert no_old["error"].startswith("Empty old_text")
    assert blank["error"].startswith("The edit leaves the file empty")
    assert turn.wizard_patch == {"metric_code": edited}


_NO_RULES_TOML = ASCENT_TOML_EXAMPLE.replace('instructions = "docs/intent.md"\n', "").replace(
    'illegal = "docs/rules.md"\n', ""
)


@pytest.mark.asyncio
async def test_write_and_edit_refuse_an_ascent_toml_that_drops_the_intent_or_rules_file() -> None:
    """A write or edit that stops naming the intent or rules file is refused by name, and the draft keeps it (SKY-794)."""
    turn = _TurnAuthoringFlag()
    tools = _tools({**_REPO_METRIC, "ascent_toml": ASCENT_TOML_EXAMPLE}, turn)

    written = await tools[WRITE_FILE_TOOL].acall(path="ascent.toml", text=_NO_RULES_TOML)
    edited = await tools[EDIT_FILE_TOOL].acall(path="ascent.toml", old_text='illegal = "docs/rules.md"\n', new_text="")

    assert list(written) == ["error"]
    assert "drops instructions = 'docs/intent.md', [proposer] illegal = 'docs/rules.md'" in written["error"]
    assert list(edited) == ["error"]
    assert "drops [proposer] illegal = 'docs/rules.md'," in edited["error"]
    assert "docs/intent.md" not in edited["error"]
    assert turn.wizard_patch == {}


@pytest.mark.asyncio
async def test_write_refuses_an_ascent_toml_that_moves_a_rules_file_or_cannot_be_checked() -> None:
    """Moving a rules file to another setting, or sending text that is not TOML, is refused (fail closed)."""
    turn = _TurnAuthoringFlag()
    tools = _tools({**_REPO_METRIC, "ascent_toml": ASCENT_TOML_EXAMPLE}, turn)
    moved = ASCENT_TOML_EXAMPLE.replace('illegal = "docs/rules.md"\n', "").replace(
        "timeout = 300\n", 'timeout = 300\nillegal = "docs/rules.md"\n'
    )

    moved_result = await tools[WRITE_FILE_TOOL].acall(path="ascent.toml", text=moved)
    broken = await tools[WRITE_FILE_TOOL].acall(path="ascent.toml", text="version = 1\n[run\n")

    assert "drops [proposer] illegal = 'docs/rules.md'" in moved_result["error"]
    assert "instructions" not in moved_result["error"].split(", which")[0]
    assert broken["error"].startswith("The new ascent.toml is not valid TOML")
    assert turn.wizard_patch == {}


@pytest.mark.asyncio
async def test_the_guard_reads_the_rules_files_of_a_current_file_the_parser_refuses() -> None:
    """A current ascent.toml with a parser error still guards the rules files it names."""
    current = ASCENT_TOML_EXAMPLE.replace('entry = ["sh", "run.sh"]\n', "")
    turn = _TurnAuthoringFlag()

    result = await _tools({**_REPO_METRIC, "ascent_toml": current}, turn)[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=_VALID_TOML
    )

    assert "drops instructions = 'docs/intent.md', [proposer] illegal = 'docs/rules.md'" in result["error"]
    assert turn.wizard_patch == {}


@pytest.mark.asyncio
async def test_write_keeps_an_ascent_toml_that_still_names_the_intent_and_rules_file() -> None:
    """A rewrite that keeps both references lands; a first ascent.toml with no references lands too."""
    turn = _TurnAuthoringFlag()
    tools = _tools({**_REPO_METRIC, "ascent_toml": ASCENT_TOML_EXAMPLE}, turn)
    kept = ASCENT_TOML_EXAMPLE.replace("timeout = 300", "timeout = 120")

    result = await tools[WRITE_FILE_TOOL].acall(path="ascent.toml", text=kept)
    edited = await tools[EDIT_FILE_TOOL].acall(path="ascent.toml", old_text="target = 0.9", new_text="target = 0.8")
    first = await _tools({**_REPO_METRIC, "ascent_toml": _VALID_TOML}, _TurnAuthoringFlag())[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=_NO_RULES_TOML
    )

    assert result == {"wizard_state": {"ascent_toml": kept}}
    assert edited == {"wizard_state": {"ascent_toml": kept.replace("target = 0.9", "target = 0.8")}}
    assert first == {"wizard_state": {"ascent_toml": _NO_RULES_TOML}}


def _fake_repo_with_toml(monkeypatch: pytest.MonkeyPatch, reads: list[str]) -> None:
    """Stand in for a GitHub repository whose ascent.toml is the guide's example.

    Args:
        monkeypatch: The test's monkeypatch.
        reads: Collects every file path read from the host.
    """
    paths = ["ascent.toml", "src/app.py"]
    monkeypatch.setattr(
        workspace_tools.github,
        "tree_entries",
        lambda token, full_name, branch: {
            "entries": [{"path": path, "type": "file", "size": 10} for path in paths],
            "truncated": False,
        },
    )

    def read_text_file(token: str | None, full_name: str, branch: str, path: str, max_bytes: int) -> tuple[str, bool]:
        """Return the example for ascent.toml and a stub for any other file."""
        reads.append(path)
        return (ASCENT_TOML_EXAMPLE if path == "ascent.toml" else "x = 1\n"), False

    monkeypatch.setattr(repo_browser.github, "read_text_file", read_text_file)


@pytest.mark.asyncio
async def test_write_refuses_dropping_the_rules_file_that_the_repository_ascent_toml_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no draft ascent.toml yet, the write is checked against the repository's file."""
    _fake_repo_with_toml(monkeypatch, [])
    turn = _TurnAuthoringFlag()

    result = await _tools(_REPO_METRIC, turn, token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=_NO_RULES_TOML
    )

    assert list(result) == ["error"]
    assert "drops instructions = 'docs/intent.md', [proposer] illegal = 'docs/rules.md'" in result["error"]
    assert turn.wizard_patch == {}


@pytest.mark.asyncio
async def test_a_blank_draft_ascent_toml_still_checks_the_repository_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A draft ascent.toml of only whitespace is no file, so the write is checked against the repository's."""
    _fake_repo_with_toml(monkeypatch, [])
    turn = _TurnAuthoringFlag()

    result = await _tools({**_REPO_METRIC, "ascent_toml": "\n"}, turn, token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=_NO_RULES_TOML
    )

    assert list(result) == ["error"]
    assert "drops instructions = 'docs/intent.md', [proposer] illegal = 'docs/rules.md'" in result["error"]
    assert turn.wizard_patch == {}


@pytest.mark.asyncio
async def test_an_edit_of_a_blank_draft_ascent_toml_still_checks_the_repository_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit of a whitespace-only draft is checked against the repository's ascent.toml, not the blank draft."""
    _fake_repo_with_toml(monkeypatch, [])
    turn = _TurnAuthoringFlag()

    result = await _tools({**_REPO_METRIC, "ascent_toml": "\n"}, turn, token=lambda: "gh-token")[EDIT_FILE_TOOL].acall(
        path="ascent.toml", old_text="\n", new_text=_NO_RULES_TOML
    )

    assert list(result) == ["error"]
    assert "drops instructions = 'docs/intent.md', [proposer] illegal = 'docs/rules.md'" in result["error"]
    assert turn.wizard_patch == {}


@pytest.mark.asyncio
async def test_the_guard_reads_a_repository_chosen_in_this_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository the agent set earlier in this turn is the one the write is checked against."""
    _fake_repo_with_toml(monkeypatch, [])
    turn = _TurnAuthoringFlag()
    turn.wizard_patch["repository"] = "acme/shop"
    state = {key: value for key, value in _REPO_METRIC.items() if key != "repository"}

    result = await _tools(state, turn, token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=_NO_RULES_TOML
    )

    assert list(result) == ["error"]
    assert "drops instructions = 'docs/intent.md', [proposer] illegal = 'docs/rules.md'" in result["error"]
    assert "ascent_toml" not in turn.wizard_patch


@pytest.mark.asyncio
async def test_the_guard_checks_the_repository_file_after_the_read_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard's read of the repository's ascent.toml works after the agent used every read of its turn."""
    _fake_repo_with_toml(monkeypatch, [])
    turn = _TurnAuthoringFlag()
    tools = _tools(_REPO_METRIC, turn, token=lambda: "gh-token")
    for _ in range(repo_browser.READS_PER_TURN):
        await tools[READ_REPO_TOOL].acall(path="src/app.py")
    assert "Read limit" in (await tools[READ_REPO_TOOL].acall(path="src/app.py"))["error"]

    refused = await tools[WRITE_FILE_TOOL].acall(path="ascent.toml", text=_NO_RULES_TOML)
    kept = await tools[WRITE_FILE_TOOL].acall(path="ascent.toml", text=ASCENT_TOML_EXAMPLE)

    assert "drops instructions = 'docs/intent.md'" in refused["error"]
    assert kept["wizard_state"]["ascent_toml"] == ASCENT_TOML_EXAMPLE


@pytest.mark.asyncio
async def test_a_retry_after_a_refusal_is_refused_again_without_a_new_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal leaves the draft empty; each retry checks the same repository text and reads it only once."""
    reads: list[str] = []
    _fake_repo_with_toml(monkeypatch, reads)
    turn = _TurnAuthoringFlag()
    tools = _tools(_REPO_METRIC, turn, token=lambda: "gh-token")

    results = [
        await tools[WRITE_FILE_TOOL].acall(path="ascent.toml", text=_NO_RULES_TOML)
        for _ in range(repo_browser.READS_PER_TURN + 2)
    ]

    assert all("drops instructions = 'docs/intent.md'" in result["error"] for result in results)
    assert reads.count("ascent.toml") == 1
    assert turn.wizard_patch == {}
    assert "error" not in str(await tools[READ_REPO_TOOL].acall(path="src/app.py"))


@pytest.mark.asyncio
async def test_write_refuses_an_ascent_toml_when_the_repository_cannot_open() -> None:
    """With no draft, a repository that cannot open leaves the write unchecked, so it is refused with the reason."""
    turn = _TurnAuthoringFlag()

    result = await _tools({**_REPO_METRIC, "repo_provider": "gitlab"}, turn)[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=_VALID_TOML
    )

    assert result["error"].startswith("ascent.toml cannot be checked against the repository's file")
    assert "GitLab is not linked" in result["error"]
    # The repository stays closed for the turn, so a retry cannot work.
    assert result["error"].endswith("Ask the user.")
    assert "Try again" not in result["error"]
    assert turn.wizard_patch == {}


@pytest.mark.asyncio
async def test_a_host_read_failure_says_a_retry_can_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed host read is not cached, so its refusal tells the agent to try again."""
    _fake_repo_with_toml(monkeypatch, [])

    def refuse(token: str | None, full_name: str, branch: str, path: str, max_bytes: int) -> tuple[str, bool]:
        """Fail the way a host error does."""
        raise RuntimeError("host down")

    monkeypatch.setattr(repo_browser.github, "read_text_file", refuse)
    turn = _TurnAuthoringFlag()

    result = await _tools(_REPO_METRIC, turn, token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=_VALID_TOML
    )

    assert "the host refused the read. Try again, or ask the user." in result["error"]
    assert turn.wizard_patch == {}


@pytest.mark.asyncio
async def test_an_edit_of_the_repository_ascent_toml_reads_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no draft, the edit's base and the guard share one host read of ascent.toml."""
    reads: list[str] = []
    _fake_repo_with_toml(monkeypatch, reads)
    turn = _TurnAuthoringFlag()

    result = await _tools(_REPO_METRIC, turn, token=lambda: "gh-token")[EDIT_FILE_TOOL].acall(
        path="ascent.toml", old_text="target = 0.9", new_text="target = 0.8"
    )

    assert result["wizard_state"]["ascent_toml"] == ASCENT_TOML_EXAMPLE.replace("target = 0.9", "target = 0.8")
    assert reads == ["ascent.toml"]


@pytest.mark.parametrize("raw", ["./docs/intent.md", "docs/./intent.md", "docs//intent.md", "/docs/intent.md"])
def test_a_rules_path_the_parser_refuses_can_be_written_in_its_clean_form(raw: str) -> None:
    """The fix of a path the parser refuses keeps the same file, so the guard lets it through."""
    current = ASCENT_TOML_EXAMPLE.replace('instructions = "docs/intent.md"', f'instructions = "{raw}"')
    assert current != ASCENT_TOML_EXAMPLE

    assert workspace_tools.dropped_rule_files(current, ASCENT_TOML_EXAMPLE) is None


@pytest.mark.asyncio
async def test_write_refuses_an_ascent_toml_when_the_repository_file_is_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository ascent.toml over the size cap cannot be checked, and a retry would read the same file."""
    _fake_repo_with_toml(monkeypatch, [])
    monkeypatch.setattr(repo_browser.github, "read_text_file", lambda *args: ("x" * 10, True))
    turn = _TurnAuthoringFlag()

    result = await _tools(_REPO_METRIC, turn, token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=_VALID_TOML
    )

    assert f"larger than {DRAFT_FILE_BYTES} bytes" in result["error"]
    assert result["error"].endswith(" Ask the user.")
    assert "Try again" not in result["error"]
    assert turn.wizard_patch == {}


@pytest.mark.asyncio
async def test_a_current_file_that_is_not_toml_still_keeps_its_rules_paths() -> None:
    """A current text that is not TOML keeps the paths of its instructions and illegal lines."""
    current = 'instructions = "docs/intent.md"\n[proposer\nillegal = "docs/rules.md"\n'
    turn = _TurnAuthoringFlag()
    tools = _tools({**_REPO_METRIC, "ascent_toml": current}, turn)

    refused = await tools[WRITE_FILE_TOOL].acall(path="ascent.toml", text=_VALID_TOML)
    kept = await tools[WRITE_FILE_TOOL].acall(path="ascent.toml", text=ASCENT_TOML_EXAMPLE)

    assert "drops 'docs/intent.md', 'docs/rules.md'. The current ascent.toml is not valid TOML" in refused["error"]
    assert "names as its" not in refused["error"]
    assert kept == {"wizard_state": {"ascent_toml": ASCENT_TOML_EXAMPLE}}


def test_rule_text_in_the_format_before_sky_380_can_move_into_a_rules_file() -> None:
    """A list of rule text names no file, so the agent can make the move the parser asks for."""
    current = 'version = 1\n[proposer]\nillegal = ["no cheating"]\n'

    assert workspace_tools.dropped_rule_files(current, 'version = 1\n[proposer]\nillegal = "docs/rules.md"\n') is None


def test_an_old_instructions_table_still_names_the_kept_rules_file() -> None:
    """A valid TOML file with an old [instructions] table names its rules file in the normal refusal."""
    current = 'version = 1\n[proposer]\nillegal = "docs/rules.md"\n[instructions]\nintent = "x"\n'

    refusal = workspace_tools.dropped_rule_files(current, "version = 1\n")

    assert refusal is not None
    assert "drops [proposer] illegal = 'docs/rules.md', which the current ascent.toml names" in refusal
    assert "not valid TOML" not in refusal


def test_a_long_run_of_backslashes_does_not_stall_the_guard() -> None:
    """The escaped-key search stays linear, so a large broken draft cannot block the event loop."""
    current = 'x = "' + "\\" * 200_000 + "\n"
    started = time.monotonic()

    workspace_tools.dropped_rule_files(current, ASCENT_TOML_EXAMPLE)

    assert time.monotonic() - started < 1


def test_a_commented_rules_path_in_a_broken_current_text_is_not_called_a_named_file() -> None:
    """A raw-search match in a comment gives a refusal that does not claim the file names the path."""
    refusal = workspace_tools.dropped_rule_files('# illegal = "docs/old.md"\n[broken\n', _VALID_TOML)

    assert refusal is not None
    assert "drops 'docs/old.md'" in refusal
    assert "maybe in a comment" in refusal
    assert "names as its" not in refusal


def test_a_new_text_that_is_not_toml_is_refused_when_the_current_text_is_not_toml_either() -> None:
    """A commented-out rules path in a broken new text does not keep the broken current text's rules file."""
    refusal = workspace_tools.dropped_rule_files(
        'illegal = "docs/rules.md"\n[broken\n', '# illegal = "docs/rules.md"\n[broken\n'
    )

    assert refusal is not None
    assert "not valid TOML, so it cannot keep 'docs/rules.md'" in refusal


@pytest.mark.parametrize(
    "line",
    [
        'proposer.illegal = "rules.md"',
        '"illegal" = "rules.md"',
        "target.'a.b'.illegal = 'rules.md'",
        'proposer = { illegal = "rules.md" }',
        'illegal = """rules.md"""',
        "illegal = '''rules.md'''",
    ],
)
def test_a_current_file_that_is_not_toml_keeps_dotted_and_quoted_rules_keys(line: str) -> None:
    """A dotted, quoted, inline-table or triple-quoted ``illegal`` key in a non-TOML text still guards its path."""
    refusal = workspace_tools.dropped_rule_files(f"version = 1\n{line}\n[broken\n", "version = 1\n")

    assert refusal is not None
    assert "'rules.md'" in refusal


@pytest.mark.parametrize(
    "line",
    [
        'illegal = """\nrules.md"""',
        'illegal = "rules\\\n.md"',
        '"\\u0069llegal" = "rules.md"',
        "illegal = rules.md",
        'instructions = ["docs/intent.md"]',
        'illegal = "docs/ru\\"les.md"',
    ],
)
def test_a_rules_path_the_raw_search_cannot_read_refuses_the_write(line: str) -> None:
    """A non-TOML current text whose rules path spans lines, has an escaped key or no string, refuses any new text."""
    refusal = workspace_tools.dropped_rule_files(f"version = 1\n{line}\n[broken\n", ASCENT_TOML_EXAMPLE)

    assert refusal is not None
    assert "cannot be read" in refusal


def test_the_raw_search_decodes_basic_string_escapes() -> None:
    """A backslash escape in a non-TOML current text decodes as TOML does, so a kept path matches."""
    current = 'instructions = "docs/intent.md"\n[proposer]\nillegal = "docs\\\\rules.md"\n[broken\n'

    assert workspace_tools.dropped_rule_files(current, ASCENT_TOML_EXAMPLE) is None
    assert workspace_tools.dropped_rule_files(current, _NO_RULES_TOML) is not None


@pytest.mark.asyncio
async def test_the_raw_fallback_cleans_paths_the_way_the_parser_does() -> None:
    """A raw path with a backslash, spaces or slashes compares equal to the parser's clean path."""
    current = ASCENT_TOML_EXAMPLE.replace('entry = ["sh", "run.sh"]\n', "").replace(
        'illegal = "docs/rules.md"', 'illegal = " docs\\\\rules.md/ "'
    )
    turn = _TurnAuthoringFlag()

    result = await _tools({**_REPO_METRIC, "ascent_toml": current}, turn)[WRITE_FILE_TOOL].acall(
        path="ascent.toml", text=ASCENT_TOML_EXAMPLE
    )

    assert "docs\\\\rules.md" in current
    assert result == {"wizard_state": {"ascent_toml": ASCENT_TOML_EXAMPLE}}


class _Spec:
    """A minimal MCP tool listing entry."""

    def __init__(self, name: str) -> None:
        """Name the tool.

        Args:
            name: The tool's MCP name.
        """
        self.name = name
        self.inputSchema = {"type": "object", "properties": {}}


class _FakeSession:
    """An MCP session that only lists tools."""

    def __init__(self, names: list[str]) -> None:
        """Remember which tools to list.

        Args:
            names: The listed tool names.
        """
        self._names = names

    async def list_tools(self) -> Any:
        """List the configured tools."""
        return type("Listing", (), {"tools": [_Spec(name) for name in self._names]})()


def _mcp_tool(_session: Any, spec: _Spec) -> dspy.Tool:
    """Build a no-argument stand-in for one MCP tool.

    Args:
        _session: Ignored.
        spec: The listed tool.

    Returns:
        A tool that answers ``{"ok": true}``.
    """

    async def call(**_kwargs: Any) -> dict[str, Any]:
        """Answer like a successful route."""
        return {"ok": True}

    return dspy.Tool(call, name=spec.name, desc="test tool", args={}, arg_types={}, arg_desc={})


@pytest.mark.asyncio
async def test_setup_agent_reads_the_repository_and_writes_the_metric_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """In one turn the setup agent reads the repository, writes the scorer and replies, with no second agent."""
    monkeypatch.setattr(settings, "firecrawl_api_key", None)
    reads: list[str] = []
    _fake_github(monkeypatch, reads)

    @asynccontextmanager
    async def fake_session(mcp_url: str, *, auth_header: str | None = None) -> AsyncIterator[_FakeSession]:
        """Yield the fake session in place of a Streamable-HTTP client.

        Args:
            mcp_url: Ignored.
            auth_header: Ignored.
        """
        yield _FakeSession(["update_wizard_state", VALIDATE_CODE_TOOL])

    monkeypatch.setattr(generalist_module, "_mcp_session", fake_session)
    monkeypatch.setattr(dspy.Tool, "from_mcp_tool", staticmethod(_mcp_tool))
    lm = _ScriptedLM(
        [
            (LIST_REPO_TOOL, {"path": ""}),
            (READ_REPO_TOOL, {"path": "tests/test_app.py"}),
            (WRITE_FILE_TOOL, {"path": "metric.py", "text": _SCORER}),
            ("submit", {"assistant_message": "I wrote the scorer. Which test counts as the main one?"}),
        ]
    )
    events: list[dict] = []

    reply = await generalist_module._drive_generalist_agent(
        mcp_url="http://unused/mcp/",
        wizard_state=WizardState(**_REPO_METRIC),
        chat_history=[],
        user_message="Write the scorer.",
        # Ask mode would hold the write for the user's approval; that gate has its own tests.
        trust_mode="auto_safe",
        registry=ApprovalRegistry(),
        emit=events.append,
        lm=lm,
        reply_language="English",
        github_token=lambda: "gh-token",
    )

    assert reply == "I wrote the scorer. Which test counts as the main one?"
    setup_tools = [t["function"]["name"] for t in lm.requests[0]["tools"]]
    for name in (LIST_REPO_TOOL, READ_REPO_TOOL, WRITE_FILE_TOOL, EDIT_FILE_TOOL, VALIDATE_CODE_TOOL):
        assert name in setup_tools
    assert "hand_off_to_coding_agent" not in setup_tools
    assert len(lm.requests) == 4
    assert reads == ["README.md", "pyproject.toml", "tests/test_app.py"]
    assert "contents of README.md" in json.dumps(lm.requests[1]["messages"])

    write_end = next(e for e in events if e["event"] == "tool_end" and e["data"]["tool"] == WRITE_FILE_TOOL)
    assert write_end["data"]["result"] == {"wizard_state": {"metric_code": _SCORER}}
    assert any(e["event"] == "tool_start" and e["data"]["tool"] == READ_REPO_TOOL for e in events)


def _guide() -> str:
    """Return the Metric guide with its whitespace folded.

    Returns:
        The guide on one line.
    """
    return " ".join(METRIC_GUIDE.split())


def test_guide_has_the_run_entry_write_the_metric_reads_output() -> None:
    """The agent writes [run] entry when the metric needs the output, and the metric never runs the candidate."""
    prompt = _guide()

    assert "[run]" in prompt
    assert 'entry = "geocode.py:geocode"' in prompt
    assert "score(repo_path, case, output=None)" in prompt
    assert "and ``None`` when nothing ran, so always give it the ``None`` default" in prompt
    assert "never imports or calls the candidate's code" in prompt
    # The runner passes output only to a scorer with three positional
    # arguments, so a two-argument metric with a [run] entry scores every
    # version alike (SKY-761).
    assert (
        "With a ``[run]`` entry, ``metric.py`` defines ``score(repo_path, case, output=None)`` and scores"
        " ``output``; a metric with fewer arguments never gets the output." in prompt
    )
    assert "Only a contract with no ``[run]`` entry, only ``[target.<name>]`` stages, may define" in prompt
    assert "``from openascent import call``" in prompt
    assert "``score(repo_path)``, or ``score(repo_path, case)`` when the run has data" not in prompt


def test_guide_puts_the_test_command_in_a_script_with_the_wrapper_line() -> None:
    """A repository scored by its own tests runs them from a script whose wrapper line writes the exit code (SKY-757)."""
    prompt = _guide()

    assert 'entry = ["sh", "run.sh"]' in prompt
    assert 'entry = ["pytest", "-q"]' not in prompt
    assert (
        '``pytest -q > log.txt`` and then ``echo "{\\"output\\": {\\"exit_code\\": $?}}" > "$ASCENT_OUTPUT"``' in prompt
    )
    assert 'The metric reads ``output["exit_code"]`` and never runs the tests itself.' in prompt
    assert "junitxml" not in prompt
    assert 'output["stdout"]' not in prompt


def test_guide_teaches_the_one_call_rule_for_command_entries() -> None:
    """A command entry reads ASCENT_INPUT, writes ASCENT_OUTPUT, and its prints only reach the logs (SKY-757)."""
    prompt = _guide()

    assert 'reads ``{"input": <the case input>}`` from the JSON file at ``ASCENT_INPUT``' in prompt
    assert 'writes ``{"output": <any JSON>}`` to the JSON file at ``ASCENT_OUTPUT``' in prompt
    assert "What it prints goes only to the logs." in prompt
    assert "A nonzero exit, a missing output file or bad JSON fails the call" in prompt
    assert "a nonzero exit code is normal output" not in prompt
    assert "pytest exits 1 when some tests fail" not in prompt


def test_guide_places_metric_secrets_and_candidate_mcp_servers() -> None:
    """Metric-only services go in [metric] services (E44); the candidate's MCP servers go in an mcp file (E46)."""
    prompt = _guide()

    assert "list in each role's ``services`` every service the role calls" in prompt.lower()
    assert 'standard ``.mcp.json`` file and set ``mcp = "<path>"`` in ``[run]``' in prompt
    assert "[run] tools" not in prompt


def test_guide_writes_secrets_only_as_placeholders() -> None:
    """Services and MCP files hold ``${NAME}`` placeholders, never values, and the reply names the secrets (E56)."""
    prompt = _guide()

    assert "Write each API that a role calls as ``[services.<name>]``" in prompt
    assert 'headers = { Authorization = "Bearer ${JUDGE_KEY}" }' in prompt
    assert "Only http servers with a ``url`` are supported now" in prompt
    assert "Put placeholders such as ``${MAPS_KEY}`` only in ``headers``." in prompt
    assert "``env``" not in prompt
    assert "Never write a secret value into any file, even when the user pastes one" in prompt
    assert "tell the user to add the secret to the run" in prompt
    assert "Name every secret the run needs in your reply" in prompt
    assert "secrets = [" not in prompt


def test_guide_writes_the_rules_into_files_and_their_paths_into_the_toml() -> None:
    """The agent writes the intent and rules into markdown files and names them in ascent.toml (E48)."""
    prompt = _guide()

    assert "Never put rule text in ascent.toml" in prompt
    assert 'set ``instructions = "docs/intent.md"`` at the top of ascent.toml, before any section' in prompt
    assert "one top-level ``- `` bullet for each rule" in prompt
    assert 'set ``illegal = "docs/rules.md"`` in ``[proposer]``' in prompt
    assert "[instructions]" not in prompt
    assert "[rules]" not in prompt


def test_guide_lets_the_owner_change_protected_files_in_setup() -> None:
    """Protected paths bind only the run's versions; in setup the agent makes the owner's edit and never asks who (SKY-793)."""
    prompt = _guide()

    assert "also the files under ``[paths] protected`` and the metric's files, for example ``evaluation/``" in prompt
    assert "That protection binds only the candidate versions during a run" in prompt
    assert "In setup, the signed-in user owns the run." in prompt
    assert "make the change and tell the user what you changed. Never ask who the user is." in prompt
    assert "in ``[proposer]``. During a run, " in prompt
    assert "hidden from the agent" not in prompt


def test_guide_writes_the_cases_into_a_jsonl_file_named_in_data() -> None:
    """Cases the metric needs go in a hidden JSONL file named by [data] cases; with no cases, no [data] (E58, E40)."""
    prompt = _guide()

    assert 'one ``{"input": ..., "expected": ...}`` object on each line' in prompt
    assert 'set ``cases = "data/cases.jsonl"`` in ``[data]``' in prompt
    assert "hides the file from the run's optimizer and the candidate" in prompt
    assert "write no ``[data]`` section" in prompt


def _schema_bullets() -> dict[str, str]:
    """Split the guide's ascent.toml schema block into its bullets.

    Returns:
        Each bullet's text by the table it starts with, such as ``[run]``; ``top`` for the top-level keys.
    """
    block = METRIC_GUIDE[METRIC_GUIDE.index("only this schema.") : METRIC_GUIDE.index("The errors you must avoid most")]
    bullets: dict[str, str] = {}
    for bullet in block.split("\n  - ")[1:]:
        text = " ".join(bullet.split())
        name = "top" if text.startswith("Top level") else text[2 : text.index("``", 2)]
        bullets[name] = text
    return bullets


def test_guide_names_every_ascent_toml_section_and_key() -> None:
    """Each schema bullet lists the keys the parser accepts for its table (SKY-184)."""
    bullets = _schema_bullets()

    for section, keys in repo_constraints._SECTIONS.items():
        role_keys = repo_constraints._ROLE_KEYS if section == "proposer" else set()
        for key in keys - role_keys:
            assert f"``{key}``" in bullets[f"[{section}]"], (section, key)
    for key in repo_constraints._ROLE_KEYS:
        assert f"``{key}``" in bullets["[proposer]"], key
    for key in repo_constraints._TARGET_KEYS - repo_constraints._ROLE_KEYS:
        assert f"``{key}``" in bullets["[target.<name>]"], key
    assert "the role keys" in bullets["[target.<name>]"]
    assert "``url``" in bullets["[services.<name>]"]
    assert "``headers``" in bullets["[services.<name>]"]
    assert "``version = 1``" in bullets["top"]
    assert "``instructions" in bullets["top"]
    assert len(bullets) == len(repo_constraints._SECTIONS) + len(repo_constraints._NAMED_SECTIONS) + 1


def test_guide_example_is_a_valid_ascent_toml() -> None:
    """The example in the guide passes the parser, so the agent never copies a broken file (SKY-184)."""
    constraints = repo_constraints.parse_constraints(ASCENT_TOML_EXAMPLE.encode())

    assert ASCENT_TOML_EXAMPLE in METRIC_GUIDE
    assert constraints.run_entry == ("sh", "run.sh")
    assert constraints.editable_paths == ("src",)
    assert (constraints.outcome_target, constraints.outcome_limits) == (0.9, {"pass_rate": 0.8})


def test_guide_says_the_target_stops_the_search_and_the_run_end_checks_the_limits() -> None:
    """The guide tells the agent the run stops at the target and judges the limits at its end (SKY-692)."""
    assert "A run stops its search when the score reaches ``target``." in METRIC_GUIDE
    assert "each ``[outcome.require]`` limit" in METRIC_GUIDE
    # SKY-775: a limit follows its score's [metric.scores] direction, so it is not always a minimum.
    assert "``[metric.scores]``" in METRIC_GUIDE
    assert "a maximum for a minimize score" in METRIC_GUIDE
    assert "its minimum score" not in METRIC_GUIDE
    assert "cannot check ``[outcome.require]``" not in METRIC_GUIDE
    assert "cannot use ``[outcome]``" not in METRIC_GUIDE


def test_guide_quotes_the_parser_errors() -> None:
    """The guide names the errors the parser gives most, so the agent avoids them (SKY-184)."""
    prompt = _guide()

    assert "is not a supported setting" in prompt
    assert "[run] entry is missing" in prompt
    assert "must be a host name such as api.example.com, not a URL" in prompt


def test_guide_marks_the_example_paths_as_placeholders() -> None:
    """The agent replaces the example's paths with ones it confirmed, not docs/intent.md as is (SKY-184)."""
    prompt = _guide()

    assert "The paths in this example, such as docs/intent.md and docs/rules.md, are placeholders" in prompt


def test_metric_snapshot_shows_what_the_code_work_needs() -> None:
    """The Metric screen's snapshot carries the brief, data, current files and repository host."""
    state = WizardState(
        **_REPO_METRIC,
        task_background="CI is slow",
        data_mode="none",
        dataset_columns=["a"],
        ascent_toml="version = 1",
        repo_provider="github",
        metric_code=_SCORER,
    )
    snapshot = generalist_module.screen_snapshot(state)
    for key in (
        "task_background",
        "data_mode",
        "dataset_columns",
        "ascent_toml",
        "repo_provider",
        "github_connected",
    ):
        assert key in snapshot, key


@pytest.mark.asyncio
async def test_write_puts_any_repository_file_into_the_draft_files() -> None:
    """A file outside metric.py and ascent.toml lands in workspace_files; the turn keeps the whole map."""
    turn = _TurnAuthoringFlag()
    tools = _tools({**_REPO_METRIC, "workspace_files": {"README.md": "old"}}, turn)

    first = await tools[WRITE_FILE_TOOL].acall(path="src/app.py", text="x = 2\n")
    second = await tools[WRITE_FILE_TOOL].acall(path="/tests/test_app.py", text="def test(): ...\n")

    assert first == {"wizard_state": {"workspace_files": {"src/app.py": "x = 2\n"}}}
    assert second == {"wizard_state": {"workspace_files": {"tests/test_app.py": "def test(): ...\n"}}}
    assert turn.wizard_patch == {
        "workspace_files": {"README.md": "old", "src/app.py": "x = 2\n", "tests/test_app.py": "def test(): ...\n"}
    }


@pytest.mark.asyncio
async def test_write_refuses_files_too_large_together_for_the_run_request() -> None:
    """A write that takes the draft files and ascent.toml over the request's total cap is refused."""
    big = "x" * (DRAFT_FILE_BYTES - 10)
    count = DRAFT_FILES_TOTAL_BYTES // len(big)
    files = {f"src/f{index}.py": big for index in range(count)}
    turn = _TurnAuthoringFlag()
    tools = _tools({**_REPO_METRIC, "workspace_files": files, "ascent_toml": "version = 1\n"}, turn)

    result = await tools[WRITE_FILE_TOOL].acall(path="src/new.py", text=big)
    replaced = await tools[WRITE_FILE_TOOL].acall(path="src/f0.py", text="y = 1\n")

    assert "larger than" in result["error"]
    assert "together" in result["error"]
    assert replaced == {"wizard_state": {"workspace_files": {"src/f0.py": "y = 1\n"}}}


@pytest.mark.asyncio
async def test_edit_starts_a_repository_file_from_the_repository_then_from_the_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first edit of a repository file reads it from GitHub; the next edit starts from the draft."""
    reads: list[str] = []
    _fake_github(monkeypatch, reads)
    turn = _TurnAuthoringFlag()
    tools = _tools(_REPO_METRIC, turn, token=lambda: "gh-token")

    first = await tools[EDIT_FILE_TOOL].acall(path="tests/test_app.py", old_text="contents", new_text="text")
    second = await tools[EDIT_FILE_TOOL].acall(path="tests/test_app.py", old_text="text of", new_text="body of")
    missing = await tools[EDIT_FILE_TOOL].acall(path="src/none.py", old_text="a", new_text="b")

    assert first == {
        "wizard_state": {"workspace_base": _HEAD, "workspace_files": {"tests/test_app.py": "text of tests/test_app.py"}}
    }
    assert second == {
        "wizard_state": {"workspace_base": _HEAD, "workspace_files": {"tests/test_app.py": "body of tests/test_app.py"}}
    }
    assert "error" in missing
    assert reads.count("tests/test_app.py") == 1
    assert turn.wizard_patch == {
        "workspace_base": _HEAD,
        "workspace_files": {"tests/test_app.py": "body of tests/test_app.py"},
    }


@pytest.mark.asyncio
async def test_a_draft_written_after_a_read_records_the_commit_it_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drafts record the commit the agent read; metric.py and a write with no read record none."""
    _fake_github(monkeypatch, [])
    blind_turn = _TurnAuthoringFlag()
    blind = await _tools(_REPO_METRIC, blind_turn, token=lambda: "gh-token")[WRITE_FILE_TOOL].acall(
        path="src/app.py", text="x = 2\n"
    )
    turn = _TurnAuthoringFlag()
    tools = _tools(_REPO_METRIC, turn, token=lambda: "gh-token")

    await tools[READ_REPO_TOOL].acall(path="README.md")
    metric = await tools[WRITE_FILE_TOOL].acall(path="metric.py", text=_SCORER)
    toml = await tools[WRITE_FILE_TOOL].acall(path="ascent.toml", text=ASCENT_TOML_EXAMPLE)

    assert "workspace_base" not in blind["wizard_state"]
    assert "workspace_base" not in blind_turn.wizard_patch
    assert metric == {"wizard_state": {"metric_code": _SCORER}}
    assert toml == {"wizard_state": {"workspace_base": _HEAD, "ascent_toml": ASCENT_TOML_EXAMPLE}}
    assert turn.wizard_patch["workspace_base"] == _HEAD


@pytest.mark.parametrize(("trust_mode", "asks"), [("ask", True), ("auto_safe", False), ("yolo", False)])
def test_the_file_tools_ask_only_in_ask_mode_and_wizard_writes_never_do(trust_mode: str, asks: bool) -> None:
    """Ask mode gates the file tools; the screen's wizard writes stay ungated in every mode."""
    assert generalist_module._needs_approval(WRITE_FILE_TOOL, trust_mode) is asks
    assert generalist_module._needs_approval(EDIT_FILE_TOOL, trust_mode) is asks
    assert generalist_module._screen_needs_approval("update_wizard_state", trust_mode) is False
    assert generalist_module._screen_needs_approval("change_protected_setting", trust_mode) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_mode", ["ask", "auto_safe", "yolo"])
async def test_a_screen_write_waits_for_approval_only_in_ask_mode(trust_mode: str) -> None:
    """In Ask mode a declined write changes nothing; in Auto-safe and YOLO it lands with no approval card."""
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    turn = _TurnAuthoringFlag()
    tool = _wrap_tool_with_approval(
        _tools(_REPO_METRIC, turn)[WRITE_FILE_TOOL],
        trust_mode=trust_mode,
        registry=registry,
        emit=events.append,
        outer_loop=asyncio.get_running_loop(),
        wizard_state=WizardState(**_REPO_METRIC),
        authoring_flag=turn,
    )

    call = asyncio.create_task(tool.func._async_body(path="src/app.py", text="x = 2\n"))
    for _ in range(100):
        if call.done() or any(event["event"] == "pending_approval" for event in events):
            break
        await asyncio.sleep(0.01)
    pending = [event["data"] for event in events if event["event"] == "pending_approval"]
    if trust_mode == "ask":
        assert len(pending) == 1
        assert pending[0]["arguments"]["path"] == "src/app.py"
        assert registry.resolve(pending[0]["id"], False)
        assert await call == "User declined"
        assert turn.wizard_patch == {}
    else:
        assert pending == []
        assert await call == {"wizard_state": {"workspace_files": {"src/app.py": "x = 2\n"}}}
        assert turn.wizard_patch == {"workspace_files": {"src/app.py": "x = 2\n"}}


def test_the_router_merges_each_written_file_into_the_draft_files() -> None:
    """A file tool's patch holds one file, so the saved state keeps the files written before it."""
    state: dict[str, Any] = {"workspace_files": {"a.py": "1"}, "job_name": "x"}

    _merge_wizard_patch(state, {"wizard_state": {"workspace_files": {"b.py": "2"}}})
    _merge_wizard_patch(state, {"wizard_state": {"workspace_files": {"a.py": "3"}, "job_name": "y"}})

    assert state == {"workspace_files": {"a.py": "3", "b.py": "2"}, "job_name": "y"}


def _local_folder(monkeypatch: pytest.MonkeyPatch, root: Path) -> dict[str, Any]:
    """Make a local folder run in desktop local mode; no host may be asked anything.

    Args:
        monkeypatch: The test's monkeypatch.
        root: The folder the run optimizes.

    Returns:
        The Metric screen state of the run.
    """

    def no_host(*_args: Any, **_kwargs: Any) -> Any:
        """Fail the test on any host call."""
        raise AssertionError("a local folder run asked a repository host")

    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(workspace_tools.github, "tree_entries", no_host)
    monkeypatch.setattr(repo_browser.github, "read_text_file", no_host)
    monkeypatch.setattr(workspace_tools.repo_hosts, "resolve_commit", no_host)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# Shop\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return {
        "wizard_screen": "metric",
        "job_name": "Faster tests",
        "task_objective": "Make the test suite pass faster",
        "repo_provider": "local",
        "repo_path": str(root),
    }


def test_open_repo_picks_the_local_folder_with_no_host_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A local run opens the folder at ``repo_path`` and never asks for a token."""
    state = _local_folder(monkeypatch, tmp_path)

    def no_token() -> str | None:
        """Fail the test when the token is asked for."""
        raise AssertionError("a local folder run asked for a host token")

    browser, reason = open_repo(state, no_token)

    assert (isinstance(browser, repo_browser.LocalFolderBrowser), reason) == (True, "")
    assert browser.branch == repo_browser.folder_snapshot_id(tmp_path)
    assert "already read for you" in browser.summary()
    assert open_repo({**state, "repo_path": ""}, no_token) == (None, "No folder is chosen yet.")
    assert open_repo({**state, "repo_path": " "}, no_token) == (None, "No folder is chosen yet.")
    monkeypatch.setattr(settings, "local_mode", False)
    assert open_repo(state, no_token) == (None, "A local folder can be read only in the desktop app.")


@pytest.mark.asyncio
async def test_the_agent_reads_a_local_folder_and_never_sees_env_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The read tools list and read the folder, and a .env file stays out of both."""
    state = _local_folder(monkeypatch, tmp_path)
    tools = _tools(state, _TurnAuthoringFlag())

    tree = await tools[LIST_REPO_TOOL].acall()
    read = await tools[READ_REPO_TOOL].acall(path="src/app.py")
    env = await tools[READ_REPO_TOOL].acall(path=".env")

    assert "Local folder" in tree
    assert "app.py" in tree
    assert ".env" not in tree
    assert "SECRET" not in tree
    assert read == "   1 x = 1"
    assert "error" in env
    assert "SECRET" not in str(env)


@pytest.mark.asyncio
async def test_a_local_draft_records_the_folder_snapshot_and_leaves_the_folder_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Draft files after a read pin ``workspace_base`` to the snapshot id, not a commit; the disk keeps its text."""
    state = _local_folder(monkeypatch, tmp_path)
    snapshot = repo_browser.folder_snapshot_id(tmp_path)
    turn = _TurnAuthoringFlag()
    tools = _tools(state, turn)

    edited = await tools[EDIT_FILE_TOOL].acall(path="src/app.py", old_text="x = 1", new_text="x = 2")

    assert edited == {"wizard_state": {"workspace_base": snapshot, "workspace_files": {"src/app.py": "x = 2\n"}}}
    assert turn.wizard_patch["workspace_base"] == snapshot != _HEAD
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    again = _tools({**state, **turn.wizard_patch}, _TurnAuthoringFlag())
    assert await again[READ_REPO_TOOL].acall(path="src/app.py") == "   1 x = 1"


@pytest.mark.asyncio
async def test_a_local_folder_that_changed_after_the_drafts_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Drafts based on an older folder state cannot read the new one, because the run would refuse them."""
    state = _local_folder(monkeypatch, tmp_path)
    drafted = {**state, "workspace_base": "b" * 40, "workspace_files": {"src/app.py": "x = 2\n"}}

    result = await _tools(drafted, _TurnAuthoringFlag())[READ_REPO_TOOL].acall(path="README.md")

    assert result == {
        "error": "The folder changed after the draft files were written, so the run would refuse them. "
        "Ask the user to discard the changed files in the editor, then read the folder again."
    }
    stale_base_no_drafts = {**state, "workspace_base": "b" * 40}
    assert isinstance(
        await _tools(stale_base_no_drafts, _TurnAuthoringFlag())[READ_REPO_TOOL].acall(path="README.md"), str
    )


@pytest.mark.asyncio
async def test_the_rules_file_guard_reads_the_ascent_toml_of_a_local_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no draft ascent.toml, a write is checked against the folder's own file, as for a repository."""
    state = _local_folder(monkeypatch, tmp_path)
    (tmp_path / "ascent.toml").write_text(ASCENT_TOML_EXAMPLE, encoding="utf-8")
    turn = _TurnAuthoringFlag()

    result = await _tools(state, turn)[WRITE_FILE_TOOL].acall(path="ascent.toml", text=_NO_RULES_TOML)

    assert list(result) == ["error"]
    assert "drops instructions = 'docs/intent.md', [proposer] illegal = 'docs/rules.md'" in result["error"]
    assert turn.wizard_patch == {}


def test_the_tool_texts_say_folder_for_a_local_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A local run's tools describe a folder; a git run's tools still describe a repository."""
    local = _tools(_local_folder(monkeypatch, tmp_path), _TurnAuthoringFlag())
    git = _tools(_REPO_METRIC, _TurnAuthoringFlag())

    for name in (LIST_REPO_TOOL, READ_REPO_TOOL, WRITE_FILE_TOOL, EDIT_FILE_TOOL):
        assert "repository" not in local[name].desc
        assert "folder" in local[name].desc or "folder" in json.dumps(local[name].arg_desc)
        assert "repository" in git[name].desc
    assert local[READ_REPO_TOOL].arg_desc["path"] == "File path inside the folder, exactly as the tree shows it."


@pytest.mark.asyncio
async def test_the_read_tools_keep_a_space_that_is_part_of_a_folder_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The list tool strips only slashes, so a folder named with a trailing space opens."""
    state = _local_folder(monkeypatch, tmp_path)
    (tmp_path / "docs ").mkdir()
    (tmp_path / "docs " / "intro.md").write_text("hi\n", encoding="utf-8")
    tools = _tools(state, _TurnAuthoringFlag())

    assert "intro.md" in await tools[LIST_REPO_TOOL].acall(path="/docs /")
    assert await tools[READ_REPO_TOOL].acall(path="docs /intro.md") == "   1 hi"

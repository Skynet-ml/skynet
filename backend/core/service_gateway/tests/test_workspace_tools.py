"""Tests for the Metric screen's workspace tools: the setup agent reads the repository and writes the files."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

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
    EDIT_FILE_TOOL,
    LIST_REPO_TOOL,
    METRIC_GUIDE,
    OPTIMIZE_TOML_EXAMPLE,
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
_SCORER = "def score(repo_path):\n    return 1.0, 'ok'\n"
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
    """A refused write or edit ends with status "error"; an optimize.toml parser error alone stays "ok".

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

    tools = _tools({**_REPO_METRIC, "metric_code": _SCORER}, _TurnAuthoringFlag())
    # The gate replaces the tool's func in place, so each tool is wrapped once.
    write = wrap(tools[WRITE_FILE_TOOL])
    await write.func._async_body(path="run.py", text=_SCORER)
    await wrap(tools[EDIT_FILE_TOOL]).func._async_body(path="metric.py", old_text="nothing", new_text="x")
    await write.func._async_body(path="optimize.toml", text="version = 1\n[run]\ntimeout = 30\n")

    ends = [event["data"] for event in events if event["event"] == "tool_end"]
    assert [end["status"] for end in ends] == ["error", "error", "ok"]
    assert ends[0]["result"]["error"].startswith("run.py cannot be written")
    assert "error" in ends[1]["result"]
    assert "optimize_toml_error" in ends[2]["result"]
    assert "error" not in ends[2]["result"]


def test_repo_metric_screen_needs_only_the_scorer() -> None:
    """A repository run has no run.py, so the scorer alone completes the Metric screen."""
    assert _screen_problems("metric", WizardState(**_REPO_METRIC)) == ["metric_code must be written"]
    assert _screen_problems("metric", WizardState(**_REPO_METRIC, metric_code=_SCORER)) == []


def test_an_invalid_optimize_toml_holds_the_metric_screen() -> None:
    """The Metric screen is not done while the draft's optimize.toml fails the run's parser."""
    bad = WizardState(**_REPO_METRIC, metric_code=_SCORER, optimize_toml="version = 2")
    assert _screen_problems("metric", bad) == [
        "optimize.toml is not valid: optimize.toml: version 2 is not supported; use version = 1."
    ]
    assert (
        _screen_problems("metric", WizardState(**_REPO_METRIC, metric_code=_SCORER, optimize_toml="version = 1")) == []
    )
    assert _screen_problems("metric", WizardState(**_REPO_METRIC, metric_code=_SCORER, optimize_toml="  ")) == []


async def test_check_setup_reports_the_parser_error_and_holds_continue() -> None:
    """``check_setup`` returns the first error, and Continue refuses with it until a fix lands this turn."""
    turn = _TurnAuthoringFlag()
    state = WizardState(**{**_REPO_METRIC, "wizard_screen": "metric"}, metric_code=_SCORER, optimize_toml="x = [")
    tools = {tool.name: tool for tool in screen_nav_tools(state, turn)}

    result = await tools["check_setup"].acall()
    assert result["ok"] is False
    assert result["errors"][0]["message"].startswith("optimize.toml is not valid TOML")
    refused = await tools["continue_to_next_screen"].acall()
    assert isinstance(refused, str)
    assert "optimize.toml is not valid" in refused

    turn.wizard_patch["optimize_toml"] = "version = 1"
    assert await tools["check_setup"].acall() == {"ok": True, "errors": []}
    assert (await tools["continue_to_next_screen"].acall())["screen_nav"]["action"] == "continue"


def test_check_setup_is_offered_only_on_the_metric_screen() -> None:
    """Only the Metric screen, where optimize.toml is written, gets ``check_setup``."""
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
    with_toml, _ = open_repo({**drafted, "optimize_toml": "version = 1\n"}, lambda: "gh-token")
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
async def test_write_puts_the_file_into_the_turn_and_the_editor() -> None:
    """A write lands in the turn's patch and comes back as a wizard_state patch for the editor."""
    turn = _TurnAuthoringFlag()
    tools = _tools(_REPO_METRIC, turn)

    result = await tools[WRITE_FILE_TOOL].acall(path="metric.py", text=_SCORER)
    toml_result = await tools[WRITE_FILE_TOOL].acall(path="/optimize.toml", text=OPTIMIZE_TOML_EXAMPLE)

    assert result == {"wizard_state": {"metric_code": _SCORER}}
    assert toml_result == {"wizard_state": {"optimize_toml": OPTIMIZE_TOML_EXAMPLE}}
    assert turn.wizard_patch == {"metric_code": _SCORER, "optimize_toml": OPTIMIZE_TOML_EXAMPLE}


@pytest.mark.asyncio
async def test_write_reports_the_parser_error_of_optimize_toml() -> None:
    """A file the parser refuses still reaches the draft, with the error and its place for the agent."""
    turn = _TurnAuthoringFlag()
    toml_text = "version = 1\n[run]\ntimeout = 30\n"

    result = await _tools(_REPO_METRIC, turn)[WRITE_FILE_TOOL].acall(path="optimize.toml", text=toml_text)

    assert result["wizard_state"] == {"optimize_toml": toml_text}
    error = result["optimize_toml_error"]
    assert (error["section"], error["key"]) == ("run", "entry")
    assert "[run] entry is missing" in error["message"]
    assert turn.wizard_patch["optimize_toml"] == toml_text


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


class _Spec:
    """A minimal MCP tool listing entry."""

    def __init__(self, name: str) -> None:
        """Name the tool.

        Args:
            name: The tool's MCP name.
        """
        self.name = name


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


def test_guide_puts_the_test_command_in_the_run_entry() -> None:
    """A repository scored by its own tests runs them from [run] entry; the metric only reads the report (E40)."""
    prompt = _guide()

    assert 'entry = ["pytest", "-q", "--junitxml=out.xml"]' in prompt
    assert "only reads the saved report" in prompt
    assert "the metric never runs the tests itself" in prompt


def test_guide_treats_a_nonzero_exit_code_as_output() -> None:
    """A command entry's nonzero exit code is output that the metric judges, like pytest's 1 on failed tests (E43)."""
    prompt = _guide()

    assert "a nonzero exit code is normal output" in prompt
    assert "the metric gets ``exit_code`` and decides what it means" in prompt
    assert "pytest exits 1 when some tests fail" in prompt
    assert "partial credit" in prompt


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
    """The agent writes the intent and rules into markdown files and names them in optimize.toml (E48)."""
    prompt = _guide()

    assert "Never put rule text in optimize.toml" in prompt
    assert 'set ``instructions = "docs/intent.md"`` at the top of optimize.toml, before any section' in prompt
    assert "one top-level ``- `` bullet for each rule" in prompt
    assert 'set ``illegal = "docs/rules.md"`` in ``[proposer]``' in prompt
    assert "[instructions]" not in prompt
    assert "[rules]" not in prompt


def test_guide_writes_the_cases_into_a_jsonl_file_named_in_data() -> None:
    """Cases the metric needs go in a hidden JSONL file named by [data] cases; with no cases, no [data] (E58, E40)."""
    prompt = _guide()

    assert 'one ``{"input": ..., "expected": ...}`` object on each line' in prompt
    assert 'set ``cases = "data/cases.jsonl"`` in ``[data]``' in prompt
    assert "hides the file from the agent and the candidate" in prompt
    assert "write no ``[data]`` section" in prompt


def _schema_bullets() -> dict[str, str]:
    """Split the guide's optimize.toml schema block into its bullets.

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


def test_guide_names_every_optimize_toml_section_and_key() -> None:
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


def test_guide_example_is_a_valid_optimize_toml() -> None:
    """The example in the guide passes the parser, so the agent never copies a broken file (SKY-184)."""
    constraints = repo_constraints.parse_constraints(OPTIMIZE_TOML_EXAMPLE.encode())

    assert OPTIMIZE_TOML_EXAMPLE in METRIC_GUIDE
    assert constraints.run_entry == ("pytest", "-q", "--junitxml=out.xml")
    assert constraints.editable_paths == ("src",)
    assert (constraints.outcome_target, constraints.outcome_minimums) == (0.9, {"pass_rate": 0.8})


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
        optimize_toml="version = 1",
        repo_provider="github",
        metric_code=_SCORER,
    )
    snapshot = generalist_module.screen_snapshot(state)
    for key in (
        "task_background",
        "data_mode",
        "dataset_columns",
        "optimize_toml",
        "repo_provider",
        "github_connected",
    ):
        assert key in snapshot, key


@pytest.mark.asyncio
async def test_write_puts_any_repository_file_into_the_draft_files() -> None:
    """A file outside metric.py and optimize.toml lands in workspace_files; the turn keeps the whole map."""
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
    """A write that takes the draft files and optimize.toml over the request's total cap is refused."""
    big = "x" * (DRAFT_FILE_BYTES - 10)
    count = DRAFT_FILES_TOTAL_BYTES // len(big)
    files = {f"src/f{index}.py": big for index in range(count)}
    turn = _TurnAuthoringFlag()
    tools = _tools({**_REPO_METRIC, "workspace_files": files, "optimize_toml": "version = 1\n"}, turn)

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
    toml = await tools[WRITE_FILE_TOOL].acall(path="optimize.toml", text=OPTIMIZE_TOML_EXAMPLE)

    assert "workspace_base" not in blind["wizard_state"]
    assert "workspace_base" not in blind_turn.wizard_patch
    assert metric == {"wizard_state": {"metric_code": _SCORER}}
    assert toml == {"wizard_state": {"workspace_base": _HEAD, "optimize_toml": OPTIMIZE_TOML_EXAMPLE}}
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

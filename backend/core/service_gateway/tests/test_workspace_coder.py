"""Tests for the Metric screen's hand-off to the workspace coding agent."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import dspy
import litellm
import pytest

from core.api.errors import DomainError
from core.config import settings
from core.service_gateway.agents import generalist as generalist_module
from core.service_gateway.agents import repo_browser, workspace_coder
from core.service_gateway.agents.generalist import (
    REPO_COLUMN_MAPPING,
    SCREEN_FOCUS,
    VALIDATE_CODE_TOOL,
    ApprovalRegistry,
    WizardState,
    _screen_problems,
    _TurnAuthoringFlag,
    _wrap_tool_with_approval,
    coding_agent_tools,
    screen_tools,
    tools_for,
)
from core.service_gateway.agents.workspace_coder import CODING_AGENT_TOOL, coder_tools, open_repo

_REPO_METRIC: dict[str, Any] = {
    "wizard_screen": "metric",
    "job_name": "Faster tests",
    "task_objective": "Make the test suite pass faster",
    "artifact_kind": "repo",
    "repository": "acme/shop",
    "github_connected": True,
}
_SCORER = "def score(repo_path):\n    return 1.0, 'ok'\n"


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

    monkeypatch.setattr(workspace_coder.github, "tree_entries", tree_entries)
    monkeypatch.setattr(repo_browser.github, "read_text_file", read_text_file)


def test_repo_metric_screen_offers_validate_code_without_a_dataset() -> None:
    """A repository run has no dataset, yet its Metric screen keeps the code check."""
    assert VALIDATE_CODE_TOOL in screen_tools(tools_for(WizardState(**_REPO_METRIC)), WizardState(**_REPO_METRIC))
    dspy_metric = WizardState(wizard_screen="metric", artifact_kind="dspy_program")
    assert VALIDATE_CODE_TOOL not in screen_tools(tools_for(dspy_metric), dspy_metric)


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


def test_repo_metric_screen_needs_only_the_scorer() -> None:
    """A repository run has no run.py, so the scorer alone completes the Metric screen."""
    assert _screen_problems("metric", WizardState(**_REPO_METRIC)) == ["metric_code must be written"]
    assert _screen_problems("metric", WizardState(**_REPO_METRIC, metric_code=_SCORER)) == []
    dspy_metric = WizardState(wizard_screen="metric", artifact_kind="dspy_program", metric_code=_SCORER)
    assert _screen_problems("metric", dspy_metric) == ["run_code and metric_code must both be written"]


def test_metric_focus_hands_repository_code_to_the_coding_agent() -> None:
    """The Metric instructions send a repository run's code to the coding agent, never to fetch_url."""
    focus = SCREEN_FOCUS["metric"]
    assert CODING_AGENT_TOOL in focus
    assert "never ``fetch_url``" in focus


def test_coding_agent_is_offered_on_the_metric_screen_only() -> None:
    """The hand-off tool exists on the Metric screen and nowhere else."""
    turn = _TurnAuthoringFlag()
    tools = coding_agent_tools(WizardState(**_REPO_METRIC), turn, lm=None, emit=lambda _e: None, github_token=None)
    assert [tool.name for tool in tools] == [CODING_AGENT_TOOL]
    other = WizardState(**{**_REPO_METRIC, "wizard_screen": "budget"})
    assert coding_agent_tools(other, turn, lm=None, emit=lambda _e: None, github_token=None) == []


def test_coding_agent_reads_the_repository_only_for_a_repository_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repository tools come with a repository run; a linked caller reads with its token."""
    _fake_github(monkeypatch, [])

    def token() -> str:
        """Return the caller's token."""
        return "gh-token"

    gitlab_run = {**_REPO_METRIC, "repo_provider": "gitlab"}
    cases: list[tuple[dict[str, Any], Any]] = [
        ({**_REPO_METRIC, "artifact_kind": "dspy_program"}, token),
        (gitlab_run, None),
        (gitlab_run, lambda: None),
    ]
    for state, token_fn in cases:
        browser, reason = open_repo(state, token_fn)
        assert browser is None
        assert reason
        assert coder_tools(browser) == []

    browser, reason = open_repo(_REPO_METRIC, token)
    assert browser is not None
    assert reason == ""
    assert [tool.name for tool in coder_tools(browser)] == ["list_repo_folder", "read_repo_file"]


def test_coding_agent_reads_a_public_github_repository_without_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a GitHub link, the coding agent opens and reads the repository anonymously (SKY-568)."""
    reads: list[str] = []
    _fake_github(monkeypatch, reads, token_seen=None)
    unlinked = {**_REPO_METRIC, "github_connected": False}

    for token_fn in (None, lambda: None, lambda: ""):
        browser, reason = open_repo(unlinked, token_fn)
        assert browser is not None
        assert reason == ""
        assert [tool.name for tool in coder_tools(browser)] == ["list_repo_folder", "read_repo_file"]
    assert "README.md" in reads


def test_coding_agent_says_only_a_public_repository_reads_without_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """An anonymous listing that GitHub refuses explains that only a public repository can be read."""

    def missing(token: str | None, full_name: str, branch: str) -> dict[str, Any]:
        """Answer like GitHub for a private or missing repository."""
        raise DomainError("connectors.not_found", status=404, provider="GitHub")

    monkeypatch.setattr(workspace_coder.github, "tree_entries", missing)

    browser, reason = open_repo({**_REPO_METRIC, "github_connected": False}, None)
    linked_browser, linked_reason = open_repo(_REPO_METRIC, lambda: "gh-token")

    assert browser is None
    assert "only a public repository can be read" in reason
    assert "Connect GitHub to optimize a private repository." in reason
    assert linked_browser is None
    assert linked_reason == "GitHub refused to list acme/shop."


def test_coding_agent_reads_anonymously_when_the_token_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vault failure while loading the token falls back to an anonymous read, not a failed turn."""
    _fake_github(monkeypatch, [], token_seen=None)

    def broken_token() -> str:
        """Fail like a database error inside the vault."""
        raise RuntimeError("database unavailable")

    browser, reason = open_repo(_REPO_METRIC, broken_token)

    assert browser is not None
    assert reason == ""


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
async def test_setup_agent_resumes_from_the_coding_agents_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setup agent hands off, the coding agent reads the repository, and the setup turn goes on from its result."""
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
            (CODING_AGENT_TOOL, {"request": "Score how fast the tests pass."}),
            ("read_repo_file", {"path": "tests/test_app.py"}),
            (
                "submit",
                {
                    "run_code": "ignored for a repository run",
                    "metric_code": _SCORER,
                    "summary": "Runs the tests and scores their time.",
                    "open_questions": ["Which test counts as the main one?"],
                },
            ),
            ("submit", {"assistant_message": "I wrote the scorer. Which test counts as the main one?"}),
        ]
    )
    events: list[dict] = []

    reply = await generalist_module._drive_generalist_agent(
        mcp_url="http://unused/mcp/",
        wizard_state=WizardState(**_REPO_METRIC),
        chat_history=[],
        user_message="Write the scorer.",
        trust_mode="ask",
        registry=ApprovalRegistry(),
        emit=events.append,
        lm=lm,
        reply_language="English",
        github_token=lambda: "gh-token",
    )

    assert reply == "I wrote the scorer. Which test counts as the main one?"
    setup_tools = [t["function"]["name"] for t in lm.requests[0]["tools"]]
    assert CODING_AGENT_TOOL in setup_tools
    assert VALIDATE_CODE_TOOL in setup_tools
    coder_request = lm.requests[1]
    assert [t["function"]["name"] for t in coder_request["tools"]] == ["list_repo_folder", "read_repo_file", "submit"]
    coder_prompt = json.dumps(coder_request["messages"])
    assert "Make the test suite pass faster" in coder_prompt
    assert "contents of README.md" in coder_prompt
    assert reads == ["README.md", "pyproject.toml", "tests/test_app.py"]

    hand_off_end = next(e for e in events if e["event"] == "tool_end" and e["data"]["tool"] == CODING_AGENT_TOOL)
    result = hand_off_end["data"]["result"]
    assert result["wizard_state"] == {"metric_code": _SCORER}
    assert result["files_read"] == ["tests/test_app.py"]
    assert result["open_questions"] == ["Which test counts as the main one?"]
    assert any(e["event"] == "tool_start" and e["data"]["tool"] == "read_repo_file" for e in events)

    resumed = json.dumps(lm.requests[3]["messages"])
    assert "Runs the tests and scores their time." in resumed
    assert "tests/test_app.py" in resumed
    assert "Which test counts as the main one?" in resumed


def test_coder_prompt_has_the_run_entry_write_the_metric_reads_output() -> None:
    """The coding agent writes [run] entry when the metric needs the output, and the metric never runs the candidate."""
    prompt = " ".join(workspace_coder.CODER_PROMPT.split())

    assert "[run]" in prompt
    assert 'entry = "geocode.py:geocode"' in prompt
    assert "score(repo_path, case, output=None)" in prompt
    assert "and ``None`` when nothing ran, so always give it the ``None`` default" in prompt
    assert "never imports or calls the candidate's code" in prompt


def test_coder_prompt_puts_the_test_command_in_the_run_entry() -> None:
    """A repository scored by its own tests runs them from [run] entry; the metric only reads the report (E40)."""
    prompt = " ".join(workspace_coder.CODER_PROMPT.split())

    assert 'entry = ["pytest", "-q", "--junitxml=out.xml"]' in prompt
    assert "only reads the saved report" in prompt
    assert "the metric never runs the tests itself" in prompt


def test_coder_prompt_treats_a_nonzero_exit_code_as_output() -> None:
    """A command entry's nonzero exit code is output that the metric judges, like pytest's 1 on failed tests (E43)."""
    prompt = " ".join(workspace_coder.CODER_PROMPT.split())

    assert "a nonzero exit code is normal output" in prompt
    assert "the metric gets ``exit_code`` and decides what it means" in prompt
    assert "pytest exits 1 when some tests fail" in prompt
    assert "partial credit" in prompt


def test_coder_prompt_places_metric_secrets_and_candidate_mcp_servers() -> None:
    """Metric-only services go in [metric] services (E44); the candidate's MCP servers go in an mcp file (E46)."""
    prompt = " ".join(workspace_coder.CODER_PROMPT.split())

    assert "list in each role's ``services`` every service the role calls" in prompt.lower()
    assert 'standard ``.mcp.json`` file and set ``mcp = "<path>"`` in ``[run]``' in prompt
    assert "[run] tools" not in prompt


def test_coder_prompt_writes_secrets_only_as_placeholders() -> None:
    """Services and MCP files hold ``${NAME}`` placeholders, never values, and the summary names the secrets (E56)."""
    prompt = " ".join(workspace_coder.CODER_PROMPT.split())

    assert "Write each API that a role calls as ``[services.<name>]``" in prompt
    assert 'headers = { Authorization = "Bearer ${JUDGE_KEY}" }' in prompt
    assert "Only http servers with a ``url`` are supported now" in prompt
    assert "Put placeholders such as ``${MAPS_KEY}`` only in ``headers``." in prompt
    assert "``env``" not in prompt
    assert "Never write a secret value into any file, even when the user pastes one" in prompt
    assert "tell the user to add the secret to the run" in prompt
    assert "Name every secret the run needs in ``summary``" in prompt
    assert "secrets = [" not in prompt


def test_coder_prompt_writes_the_rules_into_files_and_their_paths_into_the_toml() -> None:
    """The coding agent writes the intent and rules into markdown files and names them in optimize.toml (E48)."""
    prompt = " ".join(workspace_coder.CODER_PROMPT.split())

    assert "Never put rule text in optimize.toml" in prompt
    assert 'set ``instructions = "docs/intent.md"`` at the top of optimize.toml, before any section' in prompt
    assert "one top-level ``- `` bullet for each rule" in prompt
    assert 'set ``illegal = "docs/rules.md"`` in ``[proposer]``' in prompt
    assert "[instructions]" not in prompt
    assert "[rules]" not in prompt


def test_coder_prompt_writes_the_cases_into_a_jsonl_file_named_in_data() -> None:
    """Cases the metric needs go in a hidden JSONL file named by [data] cases; with no cases, no [data] (E58, E40)."""
    prompt = " ".join(workspace_coder.CODER_PROMPT.split())

    assert 'one ``{"input": ..., "expected": ...}`` object on each line' in prompt
    assert 'set ``cases = "data/cases.jsonl"`` in ``[data]``' in prompt
    assert "hides the file from the agent and the candidate" in prompt
    assert "write no ``[data]`` section" in prompt

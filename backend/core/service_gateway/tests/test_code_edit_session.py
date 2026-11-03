"""Unit tests for the code agent's chat edit tools.

Covers the no-LLM surface of :class:`_CodeEditSession`: the adapter-debris
strip applied to tool arguments (minimax occasionally emits a malformed
trailing field marker) and its interaction with the per-turn edit
guardrails. Validators are monkeypatched — their subprocess round-trip is
covered elsewhere. The wizard's twin, :class:`_UnifiedEditSession`, and its
static run-code / metric contract checks are covered the same way.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from core.models.unified import UnifiedAuthoringContext
from core.service_gateway.agents import code as code_module
from core.service_gateway.agents.code import (
    _CodeEditSession,
    _unified_contracts,
    _unified_validators,
    _UnifiedEditSession,
    _validate_run_code,
    _validate_unified_metric_code,
)
from core.service_gateway.agents.parse_salvage import strip_adapter_debris

_SIG = (
    "class Step(dspy.Signature):\n"
    '    """Do the step."""\n\n'
    "    q: str = dspy.InputField()\n"
    "    a: str = dspy.OutputField()\n"
)

_NEW_SIG = (
    "class Step(dspy.Signature):\n"
    '    """Do the step carefully."""\n\n'
    "    q: str = dspy.InputField()\n"
    "    a: str = dspy.OutputField()\n"
)

_MET = "def metric(gold, pred, trace=None):\n    return 1.0\n"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("code = 1\n[[ ## completed ## ]]", "code = 1"),
        ("code = 1\n[[ ## completed ## ]", "code = 1"),
        ("code = 1\n[[ ## completed ##", "code = 1"),
        ("x = a[b[0]]", "x = a[b[0]]"),
    ],
)
def test_strip_adapter_debris(raw: str, expected: str) -> None:
    """Trailing field markers are dropped, well-formed or malformed; real code survives."""
    assert strip_adapter_debris(raw) == expected


@pytest.fixture
def events() -> list[dict]:
    """Collect the SSE events a session emits during a test."""
    return []


@pytest.fixture
def session(events: list[dict]) -> _CodeEditSession:
    """Build a session over the starter signature/metric with a capturing emitter."""
    return _CodeEditSession(signature_code=_SIG, metric_code=_MET, emit=events.append)


def test_edit_signature_strips_debris_before_validation(
    monkeypatch: pytest.MonkeyPatch, session: _CodeEditSession
) -> None:
    """A trailing malformed marker is stripped so the edit validates and applies."""
    seen: list[str] = []
    monkeypatch.setattr(code_module, "_validate_signature_code", lambda c: seen.append(c) or "")
    observation = session.edit_signature("tighten docstring", _NEW_SIG + "[[ ## completed ## ]")
    assert "replaced and validated" in observation
    assert session.signature_code == _NEW_SIG.rstrip()
    assert seen == [_NEW_SIG.rstrip()]


def test_edit_signature_debris_only_edit_is_rejected_as_identical(
    monkeypatch: pytest.MonkeyPatch, session: _CodeEditSession
) -> None:
    """Current code plus debris is recognized as a no-op, not applied as a change."""
    monkeypatch.setattr(code_module, "_validate_signature_code", lambda c: "")
    observation = session.edit_signature("noop", _SIG + "[[ ## completed ## ]]")
    assert "identical" in observation
    assert session.signature_code == _SIG


def test_edit_metric_strips_debris_before_validation(
    monkeypatch: pytest.MonkeyPatch, session: _CodeEditSession
) -> None:
    """The metric tool applies the same strip as the signature tool."""
    seen: list[str] = []
    monkeypatch.setattr(code_module, "_validate_metric_code", lambda c: seen.append(c) or "")
    new_metric = "def metric(gold, pred, trace=None):\n    return 0.5\n"
    observation = session.edit_metric("halve score", new_metric + "[[ ## completed ## ]")
    assert "replaced and validated" in observation
    assert session.metric_code == new_metric.rstrip()
    assert seen == [new_metric.rstrip()]


_RUN = "def run(artifact, input):\n    return artifact\n"
_SCORE = "def score(input, output, expected=None):\n    return 1.0, {'feedback': 'ok'}\n"


def _unified(**overrides: object) -> dict:
    """Build a wizard authoring context as the router hands it over."""
    base: dict = {
        "task": {"objective": "Answer tickets.", "artifact": {"kind": "prompt", "starting_version": "Be brief."}},
        "data_mode": "pairs",
        "input_fields": ["ticket"],
        "expected_field": "label",
        "optimizer": "auto",
        "metric_has_model": False,
    }
    return {**base, **overrides}


@pytest.fixture
def u_events() -> list[dict]:
    """Collect the SSE events a unified session emits during a test."""
    return []


@pytest.fixture
def u_session(u_events: list[dict]) -> _UnifiedEditSession:
    """Build a unified session over a starter run/metric with the real contract checks."""
    run_validator, metric_validator = _unified_validators(_unified())
    return _UnifiedEditSession(
        run_code=_RUN,
        metric_code=_SCORE,
        emit=u_events.append,
        run_validator=run_validator,
        metric_validator=metric_validator,
    )


def test_unified_edit_run_rides_signature_events(u_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """A run edit publishes tool_start → signature_replace → tool_end ok and updates the slot."""
    new_run = "def run(artifact, input):\n    return artifact + input['ticket']\n"
    observation = u_session.edit_run("use the ticket", new_run + "[[ ## completed ## ]")
    assert "replaced" in observation
    assert u_session.run_code == new_run.rstrip()
    assert [e["event"] for e in u_events] == ["tool_start", "signature_replace", "tool_end"]
    assert u_events[0]["data"]["tool"] == "edit_run"
    assert u_events[2]["data"]["status"] == "ok"


def test_unified_edit_metric_rides_metric_events(u_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """A valid metric edit publishes metric_replace and updates the slot."""
    new_score = "def score(input, output, expected=None):\n    return 0.5, {'feedback': 'meh'}\n"
    observation = u_session.edit_metric("grade softly", new_score)
    assert "replaced" in observation
    assert u_session.metric_code == new_score.rstrip()
    assert [e["event"] for e in u_events] == ["tool_start", "metric_replace", "tool_end"]


def test_unified_second_edit_in_turn_is_rejected(u_session: _UnifiedEditSession) -> None:
    """The per-turn guard rejects a second edit of the same artifact."""
    u_session.edit_run("first", "def run(artifact, input):\n    return 1\n")
    observation = u_session.edit_run("second", "def run(artifact, input):\n    return 2\n")
    assert "already replaced" in observation
    assert "return 1" in u_session.run_code


def test_unified_identical_edit_is_rejected(u_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """Current code plus adapter debris is a no-op, not an edit."""
    observation = u_session.edit_metric("noop", _SCORE + "[[ ## completed ## ]]")
    assert "identical" in observation
    assert [e["event"] for e in u_events] == ["tool_start", "tool_end"]
    assert u_events[-1]["data"]["status"] == "error"


def test_unified_metric_in_the_wrong_shape_is_rejected(u_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """A DSPy-shaped metric never lands when the optimizer needs score(input, output)."""
    observation = u_session.edit_metric("dspy style", "def metric(gold, pred, trace=None):\n    return 1.0\n")
    assert "invalid" in observation
    assert u_session.metric_code == _SCORE
    assert "metric_replace" not in [e["event"] for e in u_events]


def test_unified_ask_user_and_set_brief_emit_events(u_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """ask emits once per turn; set_brief writes only the non-empty fields."""
    u_session.ask_user("Strict match?", '[{"label": "Yes"}, {"label": "No"}]')
    assert "already shown" in u_session.ask_user("Again?", "[]")
    u_session.set_brief("", "Tickets are short.")
    names = [e["event"] for e in u_events]
    assert names.count("ask") == 1
    assert {"event": "brief", "data": {"background": "Tickets are short."}} in u_events


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        ("def run(artifact, input):\n    return 1\n", ""),
        ("def run(artifact, input, extra=None):\n    return 1\n", ""),
        ("def run(*args):\n    return 1\n", ""),
        ("import dspy\nprogram = dspy.Predict('q -> a')\ndef run(artifact, input):\n    return program(**input)\n", ""),
        ("def run(input):\n    return 1\n", "(artifact, input)"),
        ("def go(artifact, input):\n    return 1\n", "def run"),
        ("def build_program():\n    return None\n", "def run(artifact, input)"),
        ("def run(artifact, input:\n    return 1\n", "line 1"),
        ("  \n", "empty"),
    ],
)
def test_validate_run_code(code: str, fragment: str) -> None:
    """The run check holds every artifact kind, a DSPy program included, to run(artifact, input)."""
    err = _validate_run_code(code)
    if fragment:
        assert fragment in err
    else:
        assert err == ""


@pytest.mark.parametrize(
    ("code", "has_model", "fragment"),
    [
        ("def score(input, output, expected=None):\n    return 1, {}\n", False, ""),
        ("def score(input, output):\n    return 1, {}\n", False, ""),
        ("def score(input):\n    return 1, {}\n", False, "score(input, output"),
        ("def score(input, output, expected):\n    return 1, {}\n", False, "score(input, output"),
        # The retired DSPy shapes are no longer accepted for any optimizer.
        ("def metric(gold, pred, trace=None, pred_name=None, pred_trace=None):\n    return 1\n", False, "def score"),
        ("def metric(gold, pred, trace=None):\n    return 1\n", False, "def score"),
        (
            "from skynet import llm\n\ndef score(input, output, expected=None):\n    return 1, {}\n",
            False,
            "",
        ),
        ("from skynet import llm\n\ndef score(input, output, expected=None):\n    return 1, {}\n", True, ""),
        ("from skynet import reward, observation\n\ndef score(input, output):\n    return 1, {}\n", False, ""),
        ("from skynet import service_cost\n\ndef score(input, output):\n    return 1, {}\n", False, ""),
        ("from skynet import reward, llm\n\ndef score(input, output):\n    return 1, {}\n", False, ""),
        ("import skynet\n\ndef score(input, output):\n    return 1, {}\n", False, ""),
        ("from skynet import *\n\ndef score(input, output):\n    return 1, {}\n", False, ""),
    ],
)
def test_validate_unified_metric_code(code: str, has_model: bool, fragment: str) -> None:
    """Enforce the metric signature regardless of the retired model flag."""
    _, validate = _unified_validators(_unified(metric_has_model=has_model))
    err = validate(code)
    if fragment:
        assert fragment in err
    else:
        assert err == ""


def test_model_judge_allowed_without_legacy_model_flag() -> None:
    """Allow a model judge when old clients omit the retired model flag."""
    code = "from skynet import llm\ndef score(input, output):\n    return llm('judge', model='openai/gpt-5'), {}"
    assert _validate_unified_metric_code(code) == ""
    for value in (True, False):
        _, validate = _unified_validators(_unified(metric_has_model=value))
        assert validate(code) == ""
    _, validate = _unified_validators({k: v for k, v in _unified().items() if k != "metric_has_model"})
    assert validate(code) == ""


def test_retired_metric_picker_argument_is_removed() -> None:
    """Keep legacy request fields without exposing the retired validator argument."""
    assert "metric_has_model" not in inspect.signature(_validate_unified_metric_code).parameters


@pytest.mark.parametrize("optimizer", ["auto", "dspy_gepa", "dspy_miprov2", "dspy_bootstrap_fewshot", "gepa"])
def test_metric_contract_is_the_same_for_every_optimizer(optimizer: str) -> None:
    """The authoring prompt names score(input, output, expected=None) whichever optimizer was picked.

    Args:
        optimizer: The optimizer the user picked.
    """
    _, metric_contract = _unified_contracts(_unified(optimizer=optimizer))
    _, baseline = _unified_contracts(_unified())
    assert "def score(input, output, expected=None)" in metric_contract
    assert "def metric(gold, pred" not in metric_contract
    assert metric_contract == baseline


@pytest.mark.parametrize("has_model", [True, False])
def test_metric_contract_always_offers_llm(has_model: bool) -> None:
    """Describe llm regardless of the retired model flag."""
    _, metric_contract = _unified_contracts(_unified(metric_has_model=has_model))
    assert "`from skynet import llm` calls it" in metric_contract
    assert "cannot add it" not in metric_contract


@pytest.mark.parametrize("has_model", [True, False])
def test_metric_contract_teaches_the_trusted_reward(has_model: bool) -> None:
    """Speed and cost goals go through ``reward`` and a fixed REWARD_SPEC, never the output."""
    _, metric_contract = _unified_contracts(_unified(metric_has_model=has_model))
    assert "from skynet import reward` (it needs no metric model)" in metric_contract
    assert "never read time or cost from the output" in metric_contract
    assert 'REWARD_SPEC = {"weights": {"accuracy"' in metric_contract
    assert "no cutoff, plateau, accuracy floor or time limit" in metric_contract
    assert "Never drop a case" in metric_contract


def test_repository_scorer_gets_no_reward_helper() -> None:
    """A repository scorer runs offline with no observation, so it is not offered ``reward``."""
    _, metric_contract = _unified_contracts(
        _unified(target="repo", data_mode="single", input_fields=[], expected_field=None)
    )
    assert "REWARD_SPEC" not in metric_contract


def test_run_contract_is_identical_for_every_artifact_kind() -> None:
    """A DSPy program gets the same run(artifact, input) contract as every other kind."""
    program, _ = _unified_contracts(_unified(task={"objective": "x", "artifact": {"kind": "dspy_program"}}))
    prompt, _ = _unified_contracts(_unified())
    assert program == prompt
    assert "- Entry point: define def run(artifact, input)" in prompt
    assert "build_program" not in prompt


def test_run_contract_is_one_contract_for_every_artifact() -> None:
    """Every artifact, a DSPy program included, gets run code plus a metric under one contract."""
    program, _ = _unified_contracts(_unified(task={"objective": "x", "artifact": {"kind": "dspy_program"}}))
    prompt, _ = _unified_contracts(_unified())
    deliverables = prompt.split("\n\n", 1)[0]
    assert program.startswith(deliverables)
    assert "the run code and the metric" in deliverables
    assert "validity check" in deliverables
    assert "signature and the metric" not in program
    for contract in (program, prompt):
        assert "The run code is plain python that turns one case input into an output" in contract
        assert "- Entry point: " in contract


def test_a_seed_without_the_authoring_context_fails(monkeypatch) -> None:
    """There is no Signature seed: a first draft needs the unified context or a workflow."""
    monkeypatch.setattr(code_module, "_build_agent_lm", lambda *_a, **_k: object())
    monkeypatch.setattr(code_module, "served_model_from", lambda _lm: "test-model")

    async def collect() -> list[dict]:
        """Drain the agent's event stream for a seed request with no authoring context.

        Returns:
            Every event the agent emitted.
        """
        return [
            event
            async for event in code_module.run_code_agent(
                dataset_columns=["q", "a"],
                column_roles={"q": "input", "a": "output"},
                sample_rows=[],
                user_message="",
                prior_signature="",
                prior_metric="",
                model="test-model",
            )
        ]

    events = asyncio.run(collect())
    assert [e["event"] for e in events] == ["error"]
    assert "authoring context" in events[0]["data"]["error"]


def test_run_and_metric_contracts_agree_on_a_prompt_output() -> None:
    """The run and metric are drafted in parallel, so both contracts pin a prompt's output to the reply str."""
    run_contract, metric_contract = _unified_contracts(_unified())
    assert "a prompt or text returns the reply str unchanged" in run_contract
    assert "for a prompt or text it is the reply str" in metric_contract
    assert "never call .get or getattr on it" in metric_contract


def test_unified_draft_opens_the_row_and_the_edit_reuses_it(
    u_session: _UnifiedEditSession, u_events: list[dict]
) -> None:
    """Streamed code opens the edit's row early; the finished call keeps that row and alone lands the code."""
    new_run = "def run(artifact, input):\n    return input['ticket']\n"
    u_session.draft("edit_run", 0, new_run[:10])
    u_session.draft("edit_run", 10, new_run[10:])
    u_session.edit_run("use the ticket", new_run)

    names = [e["event"] for e in u_events]
    assert names == ["tool_start", "code_draft", "code_draft", "tool_start", "signature_replace", "tool_end"]
    ids = {e["data"]["id"] for e in u_events if e["event"] in ("tool_start", "tool_end")}
    assert len(ids) == 1
    drafts = [e["data"] for e in u_events if e["event"] == "code_draft"]
    assert drafts == [
        {"source": "signature", "chunk": new_run[:10], "reset": True},
        {"source": "signature", "chunk": new_run[10:], "reset": False},
    ]
    assert u_events[3]["data"]["reason"] == "use the ticket"


def test_unified_draft_of_a_retry_opens_a_new_row(u_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """A rejected edit's retry streams into a fresh row; unknown tools never draft."""
    u_session.draft("edit_metric", 0, "def metric(gold, pred):")
    u_session.edit_metric("dspy style", "def metric(gold, pred):\n    return 1.0\n")
    u_session.draft("edit_metric", 0, "def score(")
    u_session.draft("ask_user", 0, "nope")

    starts = [e["data"]["id"] for e in u_events if e["event"] == "tool_start"]
    assert len(starts) == 3
    assert starts[0] == starts[1] != starts[2]
    assert [e["data"]["source"] for e in u_events if e["event"] == "code_draft"] == ["metric", "metric"]


def _verdicts(run_code: str, metric_code: str, files: dict[str, str]) -> list[dict]:
    """Fail ``helpers.py`` while it is missing ``def helper``; pass everything else.

    Args:
        run_code: Unused.
        metric_code: Unused.
        files: The extra files.

    Returns:
        One verdict per file.
    """
    out = [
        {"path": "run.py", "ok": True, "errors": [], "warnings": []},
        {"path": "metric.py", "ok": True, "errors": [], "warnings": []},
    ]
    for path, text in sorted(files.items()):
        broken = path == "helpers.py" and "def helper" not in text
        errors = [f"{path}:1:1 F821 Undefined name `helper`"] if broken else []
        out.append({"path": path, "ok": not broken, "errors": errors, "warnings": []})
    return out


@pytest.fixture
def ws_session(u_events: list[dict]) -> _UnifiedEditSession:
    """Build a unified session with one extra file and a scripted verifier."""
    run_validator, metric_validator = _unified_validators(_unified())
    return _UnifiedEditSession(
        run_code=_RUN,
        metric_code=_SCORE,
        emit=u_events.append,
        run_validator=run_validator,
        metric_validator=metric_validator,
        files={"notes.txt": "hi"},
        verifier=_verdicts,
    )


def test_write_file_creates_an_extra_file(ws_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """write_file adds the file, emits file_replace and closes its row ok."""
    observation = ws_session.write_file("add helper", "helpers.py", "def helper():\n    return 1\n")
    assert "created" in observation
    assert ws_session.files["helpers.py"] == "def helper():\n    return 1"
    kinds = [e["event"] for e in u_events]
    assert kinds == ["tool_start", "file_replace", "tool_end"]
    assert u_events[1]["data"] == {"path": "helpers.py", "code": "def helper():\n    return 1"}
    assert u_events[2]["data"]["status"] == "ok"
    assert u_events[0]["data"]["path"] == "helpers.py"


@pytest.mark.parametrize("path", ["/etc/passwd", "../x.py", "a/../b.py", "run.py", "metric.py", "x.exe", "skynet.py"])
def test_write_file_rejects_unsafe_or_fixed_paths(
    ws_session: _UnifiedEditSession, u_events: list[dict], path: str
) -> None:
    """Absolute, parent, fixed and non-text paths are refused and nothing changes."""
    observation = ws_session.write_file("x", path, "x = 1\n")
    assert "rejected" in observation
    assert path not in ws_session.files
    assert u_events[-1]["data"]["status"] == "error"
    assert not any(e["event"] == "file_replace" for e in u_events)


def test_write_file_rejects_python_that_does_not_parse(ws_session: _UnifiedEditSession) -> None:
    """A helper that does not parse is refused with its line."""
    observation = ws_session.write_file("x", "helpers.py", "def (:\n")
    assert "does not parse" in observation
    assert "helpers.py" not in ws_session.files


def test_delete_file_removes_extra_files_but_never_run_or_metric(
    ws_session: _UnifiedEditSession, u_events: list[dict]
) -> None:
    """Extra files can be deleted; run.py and metric.py cannot."""
    assert "deleted" in ws_session.delete_file("tidy", "notes.txt")
    assert ws_session.files == {}
    assert {"event": "file_delete", "data": {"path": "notes.txt"}} in u_events
    assert "cannot be deleted" in ws_session.delete_file("x", "run.py")
    assert "no file" in ws_session.delete_file("x", "ghost.py")


def test_open_file_echoes_the_path_and_line(ws_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """open_file opens one row that ends ok with the file and its line, and changes nothing."""
    before = (ws_session.run_code, ws_session.metric_code, ws_session.files)
    assert ws_session.open_file("metric.py", 1) == "Opened metric.py:1."
    assert [e["event"] for e in u_events] == ["tool_start", "tool_end"]
    start, end = (e["data"] for e in u_events)
    assert start["tool"] == end["tool"] == "open_file"
    assert start["path"] == end["path"] == "metric.py"
    assert end["id"] == start["id"]
    assert end["status"] == "ok"
    assert end["line"] == 1
    assert (ws_session.run_code, ws_session.metric_code, ws_session.files) == before


def test_open_file_rejects_a_file_not_in_the_workspace(ws_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """An unknown path closes the row as failed and lists the files that exist."""
    observation = ws_session.open_file("ghost.py", 3)
    assert "no file ghost.py" in observation
    assert "notes.txt" in observation
    assert "run.py" in observation
    end = u_events[-1]["data"]
    assert end["status"] == "error"
    assert "line" not in end


@pytest.mark.parametrize(
    ("line", "expected", "observation"),
    [(0, None, "Opened notes.txt."), (-4, None, "Opened notes.txt."), (99, 1, "Opened notes.txt:1.")],
)
def test_open_file_line_is_optional_and_clamped_to_the_file(
    ws_session: _UnifiedEditSession, u_events: list[dict], line: int, expected: int | None, observation: str
) -> None:
    """No line (or a non-positive one) opens the top; a line past the end lands on the last line."""
    assert ws_session.open_file(" notes.txt ", line) == observation
    end = u_events[-1]["data"]
    assert end["status"] == "ok"
    assert end.get("line") == expected


@pytest.fixture
def repo_session(u_events: list[dict]) -> _UnifiedEditSession:
    """Build a unified session for a repository run with two repository files."""
    run_validator, metric_validator = _unified_validators(_unified())
    return _UnifiedEditSession(
        run_code=_RUN,
        metric_code=_SCORE,
        emit=u_events.append,
        run_validator=run_validator,
        metric_validator=metric_validator,
        repo_files=["src/app.py", "README.md"],
    )


def test_open_file_opens_a_repository_file(repo_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """A repository path opens ok with the line asked for, which the editor clamps."""
    assert repo_session.open_file("src/app.py", 40) == "Opened src/app.py:40."
    end = u_events[-1]["data"]
    assert end["status"] == "ok"
    assert end["path"] == "src/app.py"
    assert end["line"] == 40


def test_open_file_rejects_a_path_in_neither_the_workspace_nor_the_repository(
    repo_session: _UnifiedEditSession, u_events: list[dict]
) -> None:
    """A path the repository does not list is refused with the workspace's files."""
    observation = repo_session.open_file("src/ghost.py", 2)
    assert observation == "Open rejected — there is no file src/ghost.py. Files: metric.py, run.py."
    assert u_events[-1]["data"]["status"] == "error"


def test_verify_code_reports_each_file_and_opens_a_fix(ws_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """A failed check names the file, emits per-file results and allows one more write of it."""
    ws_session.write_file("add helper", "helpers.py", "x = helper\n")
    u_events.clear()
    observation = ws_session.verify_code()
    assert "helpers.py: helpers.py:1:1 F821" in observation
    assert "run.py: OK" in observation
    result = next(e["data"] for e in u_events if e["event"] == "verify_result")
    assert result["ok"] is False
    assert [f["path"] for f in result["files"]] == ["run.py", "metric.py", "helpers.py", "notes.txt"]
    assert [e["event"] for e in u_events] == ["tool_start", "verify_result", "tool_end"]
    assert u_events[0]["data"]["tool"] == "verify_code"
    assert u_events[-1]["data"]["status"] == "error"
    assert ws_session.verified
    assert "created" not in ws_session.write_file("fix", "helpers.py", "def helper():\n    return 1\n")
    assert ws_session.files["helpers.py"].startswith("def helper")
    assert not ws_session.verified
    assert "rejected" in ws_session.write_file("again", "helpers.py", "def helper():\n    return 2\n")
    assert "All files pass" in ws_session.verify_code()


def test_verify_code_without_a_verifier_closes_its_row(u_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """With no verifier the row still closes, as failed."""
    assert "No check" in u_session.verify_code()
    assert u_events[-1]["data"] == {"id": u_events[0]["data"]["id"], "tool": "verify_code", "status": "error"}


def test_write_file_draft_streams_to_its_path(ws_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """A write_file draft opens one row for its path and the write reuses it."""
    ws_session.draft("write_file:helpers.py", 0, "def helper")
    ws_session.draft("write_file:helpers.py", 10, "():\n")
    assert [e["event"] for e in u_events] == ["tool_start", "code_draft", "code_draft"]
    assert u_events[1]["data"] == {"source": "file", "chunk": "def helper", "reset": True, "path": "helpers.py"}
    row = u_events[0]["data"]["id"]
    ws_session.write_file("add", "helpers.py", "def helper():\n    return 1\n")
    assert u_events[-1]["data"]["id"] == row


def test_write_file_draft_to_an_unsafe_path_is_dropped(ws_session: _UnifiedEditSession, u_events: list[dict]) -> None:
    """A draft for a path write_file would refuse never reaches the tree."""
    ws_session.draft("write_file:../evil.py", 0, "x")
    assert u_events == []


def test_unified_verifier_receives_reflection_model() -> None:
    """Compare omitted and declared models against the authoring context."""
    context = UnifiedAuthoringContext.model_validate(_unified(optimizer="gepa", reflection_name="x/y"))
    verify = code_module._unified_verifier(context.model_dump())
    metric = "from skynet import llm\ndef score(input, output):\n llm('a')\n llm('b', model='x/y')\n return 1, {}"
    assert not any("at most one" in error for error in verify(_RUN, metric, {})[1]["errors"])
    context.reflection_name = "other"
    verify = code_module._unified_verifier(context.model_dump())
    assert any("at most one" in error for error in verify(_RUN, metric, {})[1]["errors"])


def test_real_unified_verifier_maps_lint_to_the_file() -> None:
    """The real check flags an undefined name in the helper, not in run.py."""
    verify = code_module._unified_verifier(_unified(optimizer="gepa"))
    run = "from helpers import helper\n\ndef run(artifact, input):\n    return helper(artifact)\n"
    verdicts = {v["path"]: v for v in verify(run, _SCORE, {"helpers.py": "def helper(x):\n    return y\n"})}
    assert verdicts["run.py"]["ok"]
    assert verdicts["metric.py"]["ok"]
    assert not verdicts["helpers.py"]["ok"]
    assert "F821" in verdicts["helpers.py"]["errors"][0]


class _ScriptedReAct:
    """Stand in for the wizard's ReAct loop: keep its tools for a scripted turn."""

    def __init__(self, signature: object, *, tools: list, max_iters: int) -> None:
        """Record the bound session tools by name.

        Args:
            signature: Unused.
            tools: The session's tool methods.
            max_iters: Unused.
        """
        self.tools = {t.__name__: t for t in tools}


class _NoReplyStream:
    """Stand in for :class:`ReactReplyStream`: the scripted turn streams no tokens."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Accept the real stream's arguments."""

    def listeners(self) -> list:
        """Return no stream listeners."""
        return []


def test_a_turn_that_edits_after_its_check_ends_with_a_check(monkeypatch) -> None:
    """A check before the final run.py edit leaves that edit unchecked, so the turn checks again last."""
    monkeypatch.setattr(code_module, "ActingReActV2", _ScriptedReAct)
    monkeypatch.setattr(code_module, "ReactReplyStream", _NoReplyStream)
    monkeypatch.setattr(code_module, "_unified_verifier", lambda unified: _verdicts)

    def streamify(react: _ScriptedReAct, **kwargs: object):
        """Script the turn the browser test saw: helper, check, then run.py."""

        async def program(**inputs: object):
            """Write helpers.py, check, edit run.py, then finish."""
            react.tools["write_file"]("add helper", "helpers.py", "def helper(x):\n    return x\n")
            react.tools["verify_code"]()
            new_run = "from helpers import helper\n\ndef run(artifact, input):\n    return helper(artifact)\n"
            react.tools["edit_run"]("use helper", new_run)
            yield code_module.dspy.Prediction(reply="Done.", termination_reason="submit")

        return program

    monkeypatch.setattr(code_module.dspy, "streamify", streamify)

    async def turn() -> tuple[dict, list[dict]]:
        """Run the turn and drain its events."""
        queue: asyncio.Queue = asyncio.Queue()
        result = await code_module._run_unified_agent(
            lm=code_module.dspy.LM("openai/gpt-4o-mini"),
            unified=_unified(),
            user_message="Put the logic in a helper in helpers.py that run.py imports.",
            chat_history_json="[]",
            prior_run=_RUN,
            prior_metric=_SCORE,
            prior_validation="",
            initial_run=_RUN,
            initial_metric=_SCORE,
            reply_language="English",
            queue=queue,
        )
        await asyncio.sleep(0)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return result, events

    result, events = asyncio.run(turn())
    order = [e["event"] for e in events if e["event"] in {"file_replace", "verify_result", "signature_replace"}]
    assert order == ["file_replace", "verify_result", "signature_replace", "verify_result"]
    assert result["verification"]["ok"] is True
    assert "from helpers import helper" in result["signature_code"]


_REPO_SCORE = (
    "import subprocess\n\n\n"
    "def score(repo_path, case=None):\n"
    "    result = subprocess.run(['pytest', '-q'], cwd=repo_path, capture_output=True, text=True, timeout=600)\n"
    "    return float(result.returncode == 0), {'feedback': result.stdout[-2000:]}\n"
)


def _repo_unified() -> dict:
    """Build a repository run's authoring context as the router hands it over."""
    return _unified(target="repo", data_mode="single", input_fields=[], expected_field=None, metric_has_model=True)


def test_authoring_context_defaults_to_an_artifact_run() -> None:
    """A context that names no target is an artifact run, so existing callers keep their contract."""
    context = UnifiedAuthoringContext.model_validate({k: v for k, v in _unified().items() if k != "metric_has_model"})
    assert context.target == "artifact"
    assert UnifiedAuthoringContext.model_validate(_repo_unified()).target == "repo"


def test_repo_contracts_describe_the_scorer_offline_and_without_run_code() -> None:
    """A repository run gets no run code and an offline repository scorer, even with a metric model."""
    run_contract, metric_contract = _unified_contracts(_repo_unified())
    assert "NO run code" in run_contract
    assert "score(repo_path, case=None)" in metric_contract
    assert "subprocess" in metric_contract
    assert "timeout" in metric_contract
    assert "repo_files" in metric_contract
    assert "Never raise" in metric_contract
    assert "never import skynet" in metric_contract
    assert "from skynet import llm` calls it" not in metric_contract
    assert _unified_contracts(_unified(target="artifact")) == _unified_contracts(_unified())


@pytest.mark.parametrize(
    ("code", "ok"),
    [
        ("def score(repo_path):\n    return 1.0, {'feedback': 'ok'}\n", True),
        ("def score(repo_path, case=None):\n    return 1.0, {'feedback': 'ok'}\n", True),
        ("def score(repo_path, case):\n    return 1.0, {'feedback': 'ok'}\n", False),
        ("def score():\n    return 1.0, {'feedback': 'ok'}\n", False),
        ("def score(repo_path, case=None, extra=None):\n    return 1.0, {}\n", False),
        ("def score(*args):\n    return 1.0, {}\n", False),
        (_SCORE, False),
        ("from skynet import llm\n\ndef score(repo_path):\n    return 1.0, {'feedback': llm('x')}\n", False),
        ("def run(repo_path):\n    return 1.0\n", False),
    ],
)
def test_repo_metric_validator_accepts_only_the_repository_signature(code: str, ok: bool) -> None:
    """The repository scorer takes repo_path and an optional case, and never imports skynet."""
    run_validator, metric_validator = _unified_validators(_repo_unified())
    assert (metric_validator(code) == "") is ok
    assert run_validator("") == ""


def test_repo_verifier_checks_only_metric_py() -> None:
    """The repository check reports metric.py alone, its contract and its lint."""
    verify = code_module._unified_verifier(_repo_unified())
    assert verify("", _REPO_SCORE, {}) == [{"path": "metric.py", "ok": True, "errors": [], "warnings": []}]
    [wrong] = verify("", _SCORE, {})
    assert wrong["path"] == "metric.py"
    assert not wrong["ok"]
    [lint] = verify("", "def score(repo_path):\n    return missing, {}\n", {})
    assert any("F821" in error for error in lint["errors"])


def test_repo_runs_get_only_the_scorer_tools(repo_session: _UnifiedEditSession) -> None:
    """A repository run cannot write run code or other files; an artifact run keeps every tool."""
    repo_tools = [t.__name__ for t in code_module._unified_tools(repo_session, repo=True)]
    assert repo_tools == ["ask_user", "set_brief", "edit_metric", "verify_code", "open_file"]
    artifact_tools = [t.__name__ for t in code_module._unified_tools(repo_session, repo=False)]
    assert artifact_tools == [
        "ask_user",
        "set_brief",
        "edit_run",
        "edit_metric",
        "write_file",
        "delete_file",
        "verify_code",
        "open_file",
    ]


def test_a_repo_turn_uses_the_repository_assistant(monkeypatch) -> None:
    """A repository turn runs the repository prompt with the scorer tools, and writes only metric.py."""
    seen: dict = {}

    class _Recording(_ScriptedReAct):
        """Keep the signature the turn was built with."""

        def __init__(self, signature: object, *, tools: list, max_iters: int) -> None:
            """Record the signature and the tools.

            Args:
                signature: The turn's signature.
                tools: The session's tool methods.
                max_iters: Unused.
            """
            super().__init__(signature, tools=tools, max_iters=max_iters)
            seen["signature"] = signature
            seen["tools"] = list(self.tools)

    monkeypatch.setattr(code_module, "ActingReActV2", _Recording)
    monkeypatch.setattr(code_module, "ReactReplyStream", _NoReplyStream)

    def streamify(react: _Recording, **kwargs: object):
        """Script a turn that writes the scorer."""

        async def program(**inputs: object):
            """Write metric.py, then finish."""
            seen["inputs"] = inputs
            react.tools["edit_metric"]("run the tests", _REPO_SCORE)
            yield code_module.dspy.Prediction(reply="Done.", termination_reason="submit")

        return program

    monkeypatch.setattr(code_module.dspy, "streamify", streamify)

    async def turn() -> dict:
        """Run the turn."""
        return await code_module._run_unified_agent(
            lm=code_module.dspy.LM("openai/gpt-4o-mini"),
            unified={**_repo_unified(), "repo_files": ["tests/test_app.py"]},
            user_message="Score it by its tests.",
            chat_history_json="[]",
            prior_run="",
            prior_metric="",
            prior_validation="",
            initial_run="",
            initial_metric="",
            reply_language="English",
            queue=asyncio.Queue(),
        )

    result = asyncio.run(turn())
    assert seen["signature"] is code_module.RepoAssistant
    assert seen["tools"] == ["ask_user", "set_brief", "edit_metric", "verify_code", "open_file"]
    assert seen["inputs"]["repo_files"] == "tests/test_app.py"
    assert result["signature_code"] == ""
    assert result["metric_code"] == _REPO_SCORE.rstrip()
    assert [v["path"] for v in result["verification"]["files"]] == ["metric.py"]
    assert result["verification"]["ok"] is True

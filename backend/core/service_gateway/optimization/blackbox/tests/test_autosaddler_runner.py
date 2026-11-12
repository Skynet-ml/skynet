"""Drive the real AutoSaddler runner against the pinned upstream engine with a scripted agent."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .. import autosaddler_runner, native_runtime, repo_tree

# The runner needs the pinned upstream package on Python 3.12, which the
# backend venv does not carry; point this at an interpreter that has it.
_PYTHON_ENV = "APP_AUTOSADDLER_PYTHON"
_LEGACY_PYTHON_ENV = "SKYNET_AUTOSADDLER_PYTHON"

_WRAPPER = '''
import importlib.util, json, sys, traceback
from pathlib import Path
from autosaddler.v2.core.domain import Cost, canonical_json
from autosaddler.v2.prompting.models import SessionResult, Usage

spec = importlib.util.spec_from_file_location("autosaddler_runner", sys.argv[2])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ScriptedProvider:
    """Answer every upstream session with a schema-valid, vowel-adding patch."""

    def __init__(self, config):
        self.config = config
        Path("sessions.log").touch()

    async def run(self, request):
        kind = request.spec.kind
        context = json.loads(request.spec.workspace_files[".autosaddler/session_context.json"])
        if kind == "diagnose_patch":
            assert ".autosaddler/training_evidence.json" in request.spec.workspace_files
            components = json.loads((request.workspace / "candidate.json").read_text())
            output = {
                "schema_version": "app-autosaddler-diagnosis/v1",
                "intent": "add vowels",
                "diagnosis": "too few vowels",
                "expected_effect": "higher density",
                "updates": {name: text + "aaa" for name, text in components.items()},
            }
        elif kind == "evolve":
            output = {
                "schema_version": "app-autosaddler-evolution/v1",
                "parent_ids": [context["candidate_ids"][-1]],
                "component_sources": {},
                "rationale": "continue from the newest accepted candidate",
            }
        else:
            output = {"schema_version": "app-autosaddler-reflection/v1", "lessons": []}
        with Path("sessions.log").open("a") as log:
            log.write(kind + "\\n")
        usage = Usage(input_tokens=5, output_tokens=3, model=self.config.model)
        return SessionResult(
            status="completed",
            structured_output=output,
            raw_response=canonical_json(output),
            tool_calls=(),
            usage=(usage,),
            cost=Cost(sessions=1, input_tokens=5, output_tokens=3),
        )


runner.ClaudeAgentProvider = ScriptedProvider
original_execute = runner.execute


def execute(payload):
    """Keep the traceback of a failure visible to the test."""
    try:
        return original_execute(payload)
    except Exception:
        traceback.print_exc()
        raise


runner.execute = execute
sys.exit(runner.main())
'''


def _upstream_python() -> str | None:
    """Find an interpreter that can import the pinned upstream package.

    Returns:
        The interpreter path, or ``None`` when no candidate carries AutoSaddler on Python 3.12+.
    """
    for candidate in (os.environ.get(_PYTHON_ENV), os.environ.get(_LEGACY_PYTHON_ENV), sys.executable):
        if not candidate:
            continue
        probe = subprocess.run(
            [candidate, "-c", "import sys, autosaddler.v2.core.engine; assert sys.version_info >= (3, 12)"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    return None


def _vowel_fraction(text: str) -> float:
    """Score text by its vowel density.

    Args:
        text: Candidate text.

    Returns:
        Vowel fraction in ``[0, 1]``.
    """
    return sum(ch in "aeiou" for ch in text) / max(1, len(text))


def test_runner_climbs_with_upstream_engine_and_reports_usage(tmp_path: Path) -> None:
    """Run the pinned upstream loop end to end: sessions, parent scoring, acceptance and artifacts."""
    python = _upstream_python()
    if python is None:
        pytest.skip(f"set {_PYTHON_ENV} to a Python 3.12 interpreter with the pinned autosaddler package")
    (tmp_path / "rpc").mkdir()
    (tmp_path / "wrapper.py").write_text(_WRAPPER)
    payload = {
        "nonce": "testnonce",
        "engine_id": "autosaddler",
        "model": "claude-test",
        "sandbox": False,
        "max_token_cost": 0.05,
        "max_evals": 12,
        "max_concurrency": 1,
        "max_iterations": 2,
        "timeout_seconds": 60,
        "source": native_runtime.AUTOSADDLER_REVISION,
        "task": {
            "name": "test",
            "seed_candidate": "bcd",
            "objective": "maximize vowel density",
            "train_set": [{"id": "a"}, {"id": "b"}],
            "val_set": [{"id": "c"}],
        },
    }
    (tmp_path / "input.json").write_text(json.dumps(payload))
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONUNBUFFERED": "1",
        "ANTHROPIC_BASE_URL": "http://gateway.invalid/v1",
    }
    process = subprocess.Popen(
        [python, "wrapper.py", "input.json", autosaddler_runner.__file__],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    scored: list[tuple[str, str]] = []
    progress: list[dict[str, Any]] = []
    try:
        for line in process.stdout:
            if line.startswith("APP_NATIVE_PROGRESS testnonce "):
                progress.append(json.loads(line.split(" ", 2)[2]))
            if not line.startswith("APP_NATIVE_RPC testnonce "):
                continue
            request = json.loads(line.split(" ", 2)[2])
            scored.append((request["candidate"], request["example"]["id"]))
            response = {"score": _vowel_fraction(request["candidate"]), "info": {"feedback": "counted"}}
            (tmp_path / "rpc" / f"{request['id']}.json").write_text(json.dumps(response))
        stderr = process.stderr.read()
        result_file = tmp_path / "native_result.json"
        assert process.wait(timeout=60) == 0, (result_file.read_text() if result_file.exists() else "") + stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    result = json.loads((tmp_path / "native_result.json").read_text())
    assert "error" not in result, result
    assert result["best_candidate"] == "bcdaaaaaa"
    assert result["best_score"] == pytest.approx(_vowel_fraction("bcdaaaaaa"))
    assert result["total_evals"] == len(scored)
    assert {example for _, example in scored} == {"a", "b", "c"}
    assert result["metadata"]["iterations"] == 2
    sessions = (tmp_path / "sessions.log").read_text().split()
    assert sessions.count("diagnose_patch") == 2
    assert result["usage_by_model"]["claude-test"]["total_tokens"] == 8 * len(sessions)
    assert result["usage_complete"] is True
    assert (tmp_path / "native_artifacts.tar.gz.b64").stat().st_size > 0
    assert (tmp_path / "autosaddler-run" / "result.json").exists()
    cases = [event for event in progress if event.get("event") == "case_scored"]
    versions = [event for event in progress if "candidate" in event]
    assert versions
    assert [event["candidate_id"] for event in versions] == list(range(len(versions)))
    assert all(len(event["per_example"]) == cases[0]["total"] for event in versions)
    assert {event["candidate_id"] for event in cases} == set(range(len(versions)))
    assert all(event["example_id"].isdigit() and event["total"] >= 1 for event in cases)


def test_runner_drives_upstream_through_a_configured_harness(tmp_path: Path) -> None:
    """Run the pinned upstream loop with a non-Claude harness rendered into the workspace by upstream."""
    python = _upstream_python()
    if python is None:
        pytest.skip(f"set {_PYTHON_ENV} to a Python 3.12 interpreter with the pinned autosaddler package")
    (tmp_path / "rpc").mkdir()
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import json, os, pathlib\n"
        "assert pathlib.Path('AGENTS.md').read_text() and pathlib.Path(os.environ['APP_PROMPT_FILE']).read_text()\n"
        "schema = pathlib.Path('.autosaddler/session_output_schema.json').read_text()\n"
        "context = json.loads(pathlib.Path('.autosaddler/session_context.json').read_text())\n"
        "if 'diagnosis' in schema:\n"
        "    kind = 'diagnose_patch'\n"
        "    components = json.loads(pathlib.Path('candidate.json').read_text())\n"
        "    output = {'schema_version': 'app-autosaddler-diagnosis/v1', 'intent': 'add vowels',"
        " 'diagnosis': 'too few vowels', 'expected_effect': 'higher density',"
        " 'updates': {name: text + 'aaa' for name, text in components.items()}}\n"
        "elif 'evolution' in schema:\n"
        "    kind = 'evolve'\n"
        "    output = {'schema_version': 'app-autosaddler-evolution/v1', 'parent_ids': [context['candidate_ids'][-1]],"
        " 'component_sources': {}, 'rationale': 'continue'}\n"
        "else:\n"
        "    kind = 'reflect'\n"
        "    output = {'schema_version': 'app-autosaddler-reflection/v1', 'lessons': []}\n"
        "pathlib.Path('.autosaddler/session_output.json').write_text(json.dumps(output))\n"
        "with (pathlib.Path.home() / 'sessions.log').open('a') as log: log.write(kind + '\\n')\n"
        "print('done')\n"
    )
    payload = {
        "nonce": "testnonce",
        "engine_id": "autosaddler",
        "model": "claude-test",
        "sandbox": False,
        "max_token_cost": 0.05,
        "max_evals": 12,
        "max_concurrency": 1,
        "max_iterations": 2,
        "timeout_seconds": 60,
        "source": native_runtime.AUTOSADDLER_REVISION,
        "proposer": {
            "harness": "custom",
            "run_command": f'"{python}" "{agent}"',
            "instructions_file": "AGENTS.md",
            "output_format": "plain",
            "files": {},
            "env": {},
            "model": "claude-test",
            "price": {"input": 0.0, "output": 0.0},
        },
        "task": {
            "name": "test",
            "seed_candidate": "bcd",
            "objective": "maximize vowel density",
            "train_set": [{"id": "a"}, {"id": "b"}],
            "val_set": [{"id": "c"}],
        },
    }
    (tmp_path / "input.json").write_text(json.dumps(payload))
    # The sandbox ships only the runner and the bridge; running from the package
    # directory would shadow upstream ``autosaddler`` with Skynet's engine module.
    for name, text in native_runtime._runner_files("autosaddler").items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(
        [python, "autosaddler_runner.py", "input.json"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for line in process.stdout:
            if not line.startswith("APP_NATIVE_RPC testnonce "):
                continue
            request = json.loads(line.split(" ", 2)[2])
            response = {"score": _vowel_fraction(request["candidate"]), "info": {"feedback": "counted"}}
            (tmp_path / "rpc" / f"{request['id']}.json").write_text(json.dumps(response))
        stderr = process.stderr.read()
        result_file = tmp_path / "native_result.json"
        assert process.wait(timeout=60) == 0, (result_file.read_text() if result_file.exists() else "") + stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    result = json.loads((tmp_path / "native_result.json").read_text())
    assert "error" not in result, result
    assert result["best_candidate"] == "bcdaaaaaa"
    assert result["metadata"]["iterations"] == 2
    assert "type: custom" in (tmp_path / "autosaddler-run" / "resolved_config.yaml").read_text()
    sessions = (tmp_path / "sessions.log").read_text().split()
    assert sessions.count("diagnose_patch") == 2
    assert list((tmp_path / ".app-bridge").iterdir())


def test_runner_evolves_repository_versions_scored_by_the_parent(tmp_path: Path) -> None:
    """Each version is a real checkout the agent edits; the parent scores its patch and the best comes back."""
    python = _upstream_python()
    if python is None:
        pytest.skip(f"set {_PYTHON_ENV} to a Python 3.12 interpreter with the pinned autosaddler package")
    (tmp_path / "rpc").mkdir()
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text("x = 1\n")
    (source / "README.md").write_text("hi\n")
    archive = tmp_path / "tree.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    chunks = []
    for index, chunk in enumerate(repo_tree.archive_chunks(archive)):
        chunks.append(str(tmp_path / f"tree.{index}.b64"))
        Path(chunks[-1]).write_text(chunk)
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import json, pathlib\n"
        "schema = pathlib.Path('.autosaddler/session_output_schema.json').read_text()\n"
        "context = json.loads(pathlib.Path('.autosaddler/session_context.json').read_text())\n"
        "if 'diagnosis' in schema:\n"
        "    kind = 'diagnose_patch'\n"
        "    assert context['mutation_scope'] == ['src'] and context['readonly_paths'] == []\n"
        "    assert pathlib.Path('README.md').read_text() == 'hi\\n'\n"
        "    app = pathlib.Path('src/app.py')\n"
        "    app.write_text(app.read_text() + 'better\\n')\n"
        "    output = {'schema_version': 'app-autosaddler-diagnosis/v1', 'intent': 'improve',"
        " 'diagnosis': 'not better yet', 'expected_effect': 'higher score', 'changed_paths': ['src/app.py']}\n"
        "elif 'evolution' in schema:\n"
        "    kind = 'evolve'\n"
        "    output = {'schema_version': 'app-autosaddler-evolution/v1', 'parent_ids': [context['candidate_ids'][-1]],"
        " 'component_sources': {}, 'rationale': 'continue'}\n"
        "else:\n"
        "    kind = 'reflect'\n"
        "    output = {'schema_version': 'app-autosaddler-reflection/v1', 'lessons': []}\n"
        "pathlib.Path('.autosaddler/session_output.json').write_text(json.dumps(output))\n"
        "with (pathlib.Path.home() / 'sessions.log').open('a') as log: log.write(kind + '\\n')\n"
        "print('done')\n"
    )
    payload = {
        "nonce": "testnonce",
        "engine_id": "autosaddler",
        "model": "claude-test",
        "sandbox": False,
        "max_token_cost": 0.05,
        "max_evals": 12,
        "max_concurrency": 1,
        "max_iterations": 2,
        "timeout_seconds": 60,
        "source": native_runtime.AUTOSADDLER_REVISION,
        "proposer": {
            "harness": "custom",
            "run_command": f'"{python}" "{agent}"',
            "instructions_file": "GUIDE.md",
            "output_format": "plain",
            "files": {"harness.json": "__APP_API_KEY__"},
            "env": {},
            "model": "claude-test",
            "price": {"input": 0.0, "output": 0.0},
        },
        "repo": {"chunks": chunks, "editable_paths": ["."], "readonly_paths": []},
        "task": {
            "name": "test",
            "seed_candidate": "",
            "objective": "make it better",
            "train_set": [{"id": "a"}, {"id": "b"}],
            "val_set": [{"id": "c"}],
        },
    }
    payload["repo"]["editable_paths"] = ["src"]
    (tmp_path / "input.json").write_text(json.dumps(payload))
    for name, text in native_runtime._runner_files("autosaddler").items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONUNBUFFERED": "1",
        "APP_PROPOSER_API_KEY": "sk-secret-test",
    }
    process = subprocess.Popen(
        [python, "autosaddler_runner.py", "input.json"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    scored: list[str] = []
    try:
        for line in process.stdout:
            if not line.startswith("APP_NATIVE_RPC testnonce "):
                continue
            request = json.loads(line.split(" ", 2)[2])
            scored.append(request["candidate"])
            response = {"score": min(1.0, request["candidate"].count("+better") / 2), "info": {"feedback": "ok"}}
            (tmp_path / "rpc" / f"{request['id']}.json").write_text(json.dumps(response))
        stderr = process.stderr.read()
        result_file = tmp_path / "native_result.json"
        assert process.wait(timeout=60) == 0, (result_file.read_text() if result_file.exists() else "") + stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    result = json.loads((tmp_path / "native_result.json").read_text())
    assert "error" not in result, result
    assert scored[0] == ""
    assert all(repo_tree.patch_paths(patch) in ([], ["src/app.py"]) for patch in scored)
    assert result["best_candidate"].count("+better") == 2
    assert repo_tree.patch_paths(result["best_candidate"]) == ["src/app.py"]
    assert result["best_score"] == 1.0
    assert result["metadata"]["iterations"] == 2
    assert (tmp_path / "sessions.log").read_text().split().count("diagnose_patch") == 2


@pytest.mark.parametrize(
    ("cases", "expected"),
    [(None, [None]), ([], [None]), ([{"q": 1}], [{"q": 1}]), ([{"q": 1}, {"q": 2}], [{"q": 1}, {"q": 2}])],
)
def test_visible_examples_need_no_cases(cases: list[Any] | None, expected: list[Any]) -> None:
    """Zero cases score the candidate once; one or many are diagnosed and confirmed on the same examples.

    Args:
        cases: The run's cases, if any.
        expected: The examples both upstream splits score.
    """
    assert autosaddler_runner.visible_examples(cases) == expected


def test_a_blank_seed_is_a_valid_starting_component() -> None:
    """A run without a starting point starts AutoSaddler from an empty text instead of crashing."""
    assert autosaddler_runner.baseline_components("") == {autosaddler_runner._SINGLE_COMPONENT: ""}
    with pytest.raises(ValueError, match="seed candidate"):
        autosaddler_runner.baseline_components(None)


def _verify_readme_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on_block: Any) -> tuple[bool, str, str]:
    """Run the repository verifier on a checkout whose agent changed ``README.md``.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Replaces upstream's verdict type, absent from this interpreter.
        on_block: The verifier's block callback, or ``None``.

    Returns:
        The verdict's acceptance, check name and reason.
    """
    monkeypatch.setattr(autosaddler_runner, "GitVerificationVerdict", lambda *fields: fields)
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text("x = 1\n")
    (source / "README.md").write_text("hi\n")
    archive = tmp_path / "tree.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    chunks = []
    for index, chunk in enumerate(repo_tree.archive_chunks(archive)):
        chunks.append(tmp_path / f"tree.{index}.b64")
        chunks[-1].write_text(chunk)
    checkout = repo_tree.unpack_tree(chunks, tmp_path / "checkout")
    base = subprocess.run(
        ["git", "rev-parse", repo_tree.BASE_REF], cwd=checkout, check=True, capture_output=True, text=True
    ).stdout.strip()
    (checkout / "README.md").write_text("changed\n")
    verify = autosaddler_runner.repo_verifier(base, ["src"], [], on_block=on_block)
    accepted, check, reason, _ = verify(SimpleNamespace(workspace=checkout, changed_paths=["README.md"]))
    return accepted, check, reason


def test_a_constrained_repository_block_gives_the_illegal_action_message_and_a_run_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under a constraints file, a path block reads the fixed message and reaches the run log as ``rule.block``."""
    blocker = autosaddler_runner.rule_blocker(autosaddler_runner.EvaluatorMailbox("testnonce", 1.0))
    accepted, check, reason = _verify_readme_edit(tmp_path, monkeypatch, blocker)
    assert (accepted, check, reason) == (False, "repository-rules", repo_tree.ILLEGAL_ACTION_MESSAGE)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("APP_NATIVE_LOG testnonce ")]
    assert len(lines) == 1
    record = json.loads(lines[0].split(" ", 2)[2])
    assert record["event"] == repo_tree.RULE_BLOCK_EVENT
    assert record["source"] == "engine"
    assert record["fields"]["rule"] == repo_tree.PATH_RULE
    assert "README.md" in record["fields"]["reason"]


def test_an_unconstrained_repository_block_keeps_naming_the_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a constraints file, the verifier names the refused path and logs no rule event."""
    accepted, check, reason = _verify_readme_edit(tmp_path, monkeypatch, None)
    assert (accepted, check) == (False, "repository-rules")
    assert "README.md" in reason
    assert "APP_NATIVE_LOG" not in capsys.readouterr().out


def test_a_development_aggregate_with_a_crashed_case_never_stops_or_leads() -> None:
    """A filled mean with a crashed case is not best and does not stop the run (SKY-367)."""
    emitted: list[Any] = []
    evaluator = SimpleNamespace(
        best_development=None,
        stop_at_score=0.5,
        versions={"c1": 1, "c2": 2},
        case_ids={"a": "0", "b": "1"},
        mailbox=SimpleNamespace(total_evals=2, emit=lambda *args: emitted.append(args)),
    )
    crashed = [SimpleNamespace(case_id="a", score=0.9), SimpleNamespace(case_id="b", score=None)]

    autosaddler_runner.AppEvaluator._report_development(evaluator, "c1", "text one", 0.9, crashed)

    assert evaluator.best_development is None
    assert emitted == []
    scored = [SimpleNamespace(case_id="a", score=0.9), SimpleNamespace(case_id="b", score=0.7)]
    with pytest.raises(autosaddler_runner.TargetReached):
        autosaddler_runner.AppEvaluator._report_development(evaluator, "c2", "text two", 0.8, scored)
    assert evaluator.best_development["best_candidate"] == "text two"


def test_harness_sessions_send_each_refusal_to_the_run_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pass each hidden-path refusal of a harness session to the run log as ``proposer.refusal``."""
    outcome = autosaddler_runner.harness_bridge.SessionOutcome(
        text=None,
        stdout="",
        stderr="",
        returncode=0,
        usage={},
        cost_usd=0.0,
        duration_seconds=1.0,
        timed_out=True,
        refusals=[{"tool": "read", "kind": "scoring"}],
    )
    monkeypatch.setattr(autosaddler_runner.harness_bridge, "run_session", lambda *args, **kwargs: outcome)
    monkeypatch.setenv("HOME", str(tmp_path))
    logger = autosaddler_runner.refusal_logger(autosaddler_runner.EvaluatorMailbox("testnonce", 1.0))
    transport = autosaddler_runner.HarnessTransport({}, "m1", logger)
    session = SimpleNamespace(workspace=tmp_path, task_prompt="go", session_id="s1")

    with pytest.raises(TimeoutError):
        asyncio.run(transport.run(session, 1.0))

    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("APP_NATIVE_LOG testnonce ")]
    assert len(lines) == 1
    record = json.loads(lines[0].split(" ", 2)[2])
    assert record["event"] == "proposer.refusal"
    assert record["fields"] == {"tool": "read", "kind": "scoring"}

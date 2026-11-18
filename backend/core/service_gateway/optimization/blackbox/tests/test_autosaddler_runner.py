"""Drive the real AutoSaddler runner against the pinned upstream engine with a scripted agent."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .. import autosaddler_runner, harness_bridge, native_runtime, repo_tree

# The runner needs the pinned upstream package on Python 3.12, which the
# backend venv does not carry; point this at an interpreter that has it.
_PYTHON_ENV = "APP_AUTOSADDLER_PYTHON"
_LEGACY_PYTHON_ENV = "SKYNET_AUTOSADDLER_PYTHON"

_WRAPPER = '''
import importlib.util, json, sys, traceback
from pathlib import Path
from types import SimpleNamespace
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


runner.harness_provider = lambda proposer, model, on_refusal=None: ScriptedProvider(SimpleNamespace(model=model))
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


def _scripted_payload(seed: str, train_set: list[dict[str, str]], val_set: list[dict[str, str]]) -> dict[str, Any]:
    """Build a runner payload whose proposer the wrapper replaces with the scripted provider.

    Args:
        seed: Seed candidate text.
        train_set: Training examples.
        val_set: Validation examples.

    Returns:
        The payload.
    """
    return {
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
        "proposer": {"harness": "custom"},
        "task": {
            "name": "test",
            "seed_candidate": seed,
            "objective": "maximize vowel density",
            "train_set": train_set,
            "val_set": val_set,
        },
    }


def _run_scripted(
    tmp_path: Path, python: str, payload: dict[str, Any], respond: Callable[[str, str], dict[str, Any]]
) -> tuple[dict[str, Any], list[tuple[str, str]], list[dict[str, Any]]]:
    """Run the runner under the scripted wrapper and answer each scorer request as the parent does.

    Args:
        tmp_path: Working folder of the run.
        python: Interpreter with the pinned upstream package.
        payload: Runner payload.
        respond: Builds the parent's response from the candidate and the example id.

    Returns:
        The runner result, each scored ``(candidate, example id)`` pair, and the progress events.
    """
    (tmp_path / "rpc").mkdir()
    (tmp_path / "wrapper.py").write_text(_WRAPPER)
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
            response = respond(request["candidate"], request["example"]["id"])
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
    return result, scored, progress


def test_runner_climbs_with_upstream_engine_and_reports_usage(tmp_path: Path) -> None:
    """Run the pinned upstream loop end to end: sessions, parent scoring, acceptance and artifacts."""
    python = _upstream_python()
    if python is None:
        pytest.skip(f"set {_PYTHON_ENV} to a Python 3.12 interpreter with the pinned autosaddler package")
    payload = _scripted_payload("bcd", [{"id": "a"}, {"id": "b"}], [{"id": "c"}])

    result, scored, progress = _run_scripted(
        tmp_path,
        python,
        payload,
        lambda candidate, _example: {"score": _vowel_fraction(candidate), "info": {"feedback": "counted"}},
    )

    assert result["best_candidate"] == "bcdaaaaaa"
    assert result["best_score"] == pytest.approx(_vowel_fraction("bcdaaaaaa"))
    assert result["total_evals"] == len(scored)
    assert {example for _, example in scored} == {"a", "b"}
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


@pytest.mark.parametrize(
    ("seed_crashes", "crashed_cases"),
    [(False, {"b"}), (True, {"b"}), (True, {"a", "b"})],
    ids=["child-crashes", "every-version-crashes", "every-case-crashes"],
)
def test_upstream_run_finishes_when_a_train_case_crashes(
    tmp_path: Path, seed_crashes: bool, crashed_cases: set[str]
) -> None:
    """A crashed case gets upstream's floor: the run finishes, keeps the scored seed and never shows the floor (SKY-751).

    Args:
        tmp_path: Working folder of the run.
        seed_crashes: Whether the seed also crashes, so no version scores every case.
        crashed_cases: Cases that crash. On every case, upstream sums several floors.
    """
    python = _upstream_python()
    if python is None:
        pytest.skip(f"set {_PYTHON_ENV} to a Python 3.12 interpreter with the pinned autosaddler package")
    seed = "bcde"

    def respond(candidate: str, example: str) -> dict[str, Any]:
        """Crash every child, and the seed when asked, on the crashed cases; score the rest by vowel density.

        Args:
            candidate: Candidate text.
            example: Example id.

        Returns:
            The parent's response.
        """
        if example in crashed_cases and (candidate != seed or seed_crashes):
            return {"score": None, "info": {"feedback": "Run failed: boom", "status": "run_failed"}}
        return {"score": _vowel_fraction(candidate), "info": {"feedback": "counted"}}

    result, scored, progress = _run_scripted(
        tmp_path, python, _scripted_payload(seed, [{"id": "a"}, {"id": "b"}], []), respond
    )

    assert any(candidate != seed and example in crashed_cases for candidate, example in scored)
    assert result["best_candidate"] == seed
    expected = None if seed_crashes else pytest.approx(_vowel_fraction(seed))
    assert result["best_score"] == expected
    assert result["metadata"]["selection"]["development_score"] == expected
    shown = [event["score"] for event in progress if "score" in event]
    shown += [score for event in progress for _, score in event.get("per_example", [])]
    assert all(0.0 <= score <= 1.0 for score in shown)


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
    """A mean with a crashed case is not best, shows no score and does not stop the run (SKY-367, SKY-751)."""
    emitted: list[Any] = []
    evaluator = SimpleNamespace(
        best_development=None,
        no_score_candidates=set(),
        stop_at_score=0.5,
        versions={"c1": 1, "c2": 2},
        case_ids={"a": "0", "b": "1"},
        mailbox=SimpleNamespace(total_evals=2, emit=lambda *args: emitted.append(args)),
    )
    floor = autosaddler_runner._NO_SCORE_FLOOR
    crashed = [SimpleNamespace(case_id="a", score=0.9), SimpleNamespace(case_id="b", score=floor)]

    autosaddler_runner.AppEvaluator._report_development(evaluator, "c1", "text one", (0.9 + floor) / 2, crashed)

    assert evaluator.best_development is None
    assert emitted == []
    assert evaluator.no_score_candidates == {"c1"}
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


# The setup marks its own output; the fake proposer logs whether that output
# is in the folder it runs in, and prints a reply.
_SESSION_SETUP = """
import os, pathlib, sys
pathlib.Path("setup-out").mkdir()
pathlib.Path("setup-out/ready").write_text("ok")
with open(sys.argv[1], "a") as log:
    log.write(f"setup {os.getcwd()}\\n")
"""
_SESSION_AGENT = """
import os, pathlib, sys
with open(sys.argv[1], "a") as log:
    log.write(f"agent {os.getcwd()} {pathlib.Path('setup-out/ready').exists()}\\n")
print("done")
"""


def test_each_autosaddler_session_worktree_gets_the_files_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream gives each session a new worktree, so the setup runs in each one, where the command runs (SKY-753).

    Args:
        tmp_path: Guest folder.
        monkeypatch: Working folder, home folder and the upstream classes the guest builds.
    """
    tree = io.BytesIO()
    with tarfile.open(fileobj=tree, mode="w:gz") as packed:
        info = tarfile.TarInfo("src.txt")
        info.size = 1
        packed.addfile(info, io.BytesIO(b"v"))
    chunk = tmp_path / "chunk-0"
    chunk.write_text(base64.b64encode(tree.getvalue()).decode("ascii"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(harness_bridge.KEY_ENV, "k")
    monkeypatch.setattr(autosaddler_runner, "GitHarnessSpace", SimpleNamespace)
    monkeypatch.setattr(autosaddler_runner, "SessionUsage", SimpleNamespace)
    monkeypatch.setattr(autosaddler_runner, "TransportOutcome", SimpleNamespace)
    log = tmp_path / "proposer.log"
    proposer = {
        "run_command": shlex.join([sys.executable, "-c", _SESSION_AGENT, str(log)]),
        "output_format": "plain",
        "files": {},
        "env": {},
        "file_command": True,
        "setup": [sys.executable, "-c", _SESSION_SETUP, str(log)],
    }
    repo = {"chunks": [str(chunk)], "editable_paths": ["."], "readonly_paths": []}
    mailbox = autosaddler_runner.EvaluatorMailbox("testnonce", 1.0)

    space, _base = autosaddler_runner.repo_space(repo, "", tmp_path / "run", proposer, mailbox)
    transport = autosaddler_runner.HarnessTransport(proposer, "m1")
    worktrees = [tmp_path / "run" / "worktrees" / name for name in ("w1", "w2")]
    for index, worktree in enumerate(worktrees):
        subprocess.run(
            ["git", "worktree", "add", "--quiet", "--detach", str(worktree), space.base_revision],
            cwd=space.source_repo,
            check=True,
        )
        session = SimpleNamespace(workspace=worktree, task_prompt="Improve it.", session_id=f"s{index}")
        asyncio.run(transport.run(session, 30.0))

    assert log.read_text().splitlines() == [
        line for worktree in worktrees for line in (f"setup {worktree}", f"agent {worktree} True")
    ]
    assert not (space.source_repo / "setup-out").exists()
    for worktree in worktrees:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert status == ""


def test_the_runs_stop_kills_a_setup_that_an_in_process_session_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AutoSaddler runs sessions in its own process, without the bridge's SIGTERM handler; its stop sweep ends the setup.

    Args:
        tmp_path: The checkout and the setup's pid file.
        monkeypatch: Home folder and the proposer key.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "a.txt").write_text("tracked")
    for arguments in (["init", "--quiet"], ["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", "base"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments], cwd=checkout, check=True
        )
    pid_file = tmp_path / "setup.pid"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(harness_bridge.KEY_ENV, "k")
    proposer = {
        "run_command": "echo done",
        "output_format": "plain",
        "files": {},
        "env": {},
        "setup": [
            sys.executable,
            "-c",
            f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)",
        ],
    }
    transport = autosaddler_runner.HarnessTransport(proposer, "m1")
    session = SimpleNamespace(workspace=checkout, task_prompt="x", session_id="s0")
    errors: list[BaseException] = []

    def drive() -> None:
        """Run the session and keep the error it ends with."""
        try:
            asyncio.run(transport.run(session, 120.0))
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    deadline = time.monotonic() + 20
    while not pid_file.exists() or not pid_file.read_text():
        assert time.monotonic() < deadline, "the setup did not start"
        time.sleep(0.05)
    setup_pid = int(pid_file.read_text())
    try:
        autosaddler_runner._stop_children()
        worker.join(timeout=20)

        assert not worker.is_alive()
        assert len(errors) == 1
        assert "[proposer] setup command in optimize.toml" in str(errors[0])
        with pytest.raises(ProcessLookupError):
            os.kill(setup_pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(setup_pid, signal.SIGKILL)


def _evaluation(
    scores: dict[str, float | None], failed: frozenset[str] = frozenset(), split: str = "train"
) -> SimpleNamespace:
    """Build an upstream-shaped evaluation with one observation per case.

    Args:
        scores: Each case's score; ``None`` is an invalid observation, as upstream records a non-finite score.
        failed: Cases whose scorer feedback has the run-failed status.
        split: Upstream split of the evaluation.

    Returns:
        An evaluation with upstream's fields and aggregate over the valid cases.
    """
    observations = [
        SimpleNamespace(
            case_id=case_id,
            repetition=0,
            score=score,
            is_valid=score is not None,
            metadata={
                "scorer_feedback": {"feedback": "Run failed: boom", "status": "run_failed"}
                if case_id in failed
                else {"feedback": "ok"}
            },
        )
        for case_id, score in scores.items()
    ]
    valid = [score for score in scores.values() if score is not None]
    return SimpleNamespace(
        aggregate_score=sum(valid) / len(valid) if valid else None,
        requested_case_ids=tuple(scores),
        observations=observations,
        split=split,
    )


def _three(aggregate: float, crashed: bool, split: str = "train") -> SimpleNamespace:
    """Build an evaluation over cases ``a``, ``b`` and ``c``, where ``c`` may have a failed [run] entry.

    Args:
        aggregate: Score of every valid case, so also upstream's mean.
        crashed: Whether case ``c`` had a failed [run] entry.
        split: Upstream split of the evaluation.

    Returns:
        The evaluation.
    """
    if crashed:
        return _evaluation({"a": aggregate, "b": aggregate, "c": None}, frozenset({"c"}), split)
    return _evaluation({"a": aggregate, "b": aggregate, "c": aggregate}, split=split)


class _MeanRanking:
    """Rank like upstream: the highest mean over the valid cases wins, and no mean is an error."""

    def select(self, candidates: list[tuple[Any, Any]]) -> tuple[Any, Any]:
        """Select the pair with the highest aggregate.

        Args:
            candidates: ``(candidate, evaluation)`` pairs.

        Returns:
            The selected pair.

        Raises:
            ValueError: A pair has no aggregate, as upstream raises.
        """
        if any(evaluation.aggregate_score is None for _, evaluation in candidates):
            raise ValueError("Ranking requires valid development aggregates")
        return max(candidates, key=lambda item: item[1].aggregate_score)


class _StrictImprovement:
    """Accept like upstream: a higher mean over the matched valid cases wins, and no matched case is an error."""

    def compare(self, parent: Any, child: Any) -> SimpleNamespace:
        """Accept the child when its mean over the matched valid cases is higher.

        Args:
            parent: Parent evaluation.
            child: Child evaluation.

        Returns:
            A verdict with ``accepted``.

        Raises:
            ValueError: No case is valid on both sides, as upstream raises.
        """
        pairs = [
            (before.score, after.score)
            for before, after in zip(parent.observations, child.observations, strict=True)
            if before.is_valid and after.is_valid
        ]
        if not pairs:
            raise ValueError("Acceptance has no matched valid observations")
        return SimpleNamespace(accepted=sum(after for _, after in pairs) > sum(before for before, _ in pairs))


def _acceptance(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build the wrapped acceptance with upstream's verdict type stood in.

    Args:
        monkeypatch: Stands in for upstream's domain module, which needs Python 3.12.

    Returns:
        The acceptance policy the runner uses.
    """
    monkeypatch.setattr(autosaddler_runner, "as_domain", SimpleNamespace(PatchVerdict=SimpleNamespace))
    return autosaddler_runner.NoScoreRejectedAcceptance(_StrictImprovement())


def test_ranking_never_selects_a_version_with_a_failed_run_case() -> None:
    """A version that crashed on one case and scored high elsewhere loses to a lower, fully scored one (SKY-751)."""
    ranking = autosaddler_runner.NoScoreLastRanking(_MeanRanking())

    assert ranking.select([("crashed", _three(0.9, True)), ("scored", _three(0.3, False))])[0] == "scored"
    assert ranking.select([("crashed", _three(0.9, True)), ("low", _three(0.1, True))])[0] == "crashed"


def test_ranking_returns_the_seed_when_no_version_scored_every_case() -> None:
    """With no fully scored version, ranking returns the seed, which the app reports with no score (SKY-751)."""
    ranking = autosaddler_runner.NoScoreLastRanking(_MeanRanking())
    nothing = _evaluation({"a": None}, frozenset({"a"}), "development")
    partial = _evaluation({"a": 0.4, "b": None}, frozenset({"b"}), "development")
    metric_error = _evaluation({"a": 0.9, "b": None}, split="development")

    assert ranking.select([("seed", nothing), ("child", partial)])[0] == "seed"
    assert ranking.select([("seed", nothing), ("child", nothing)])[0] == "seed"
    assert ranking.select([("seed", metric_error), ("child", partial)])[0] == "seed"
    assert ranking.select([("seed", _three(0.1, False, "development")), ("child", metric_error)])[0] == "seed"


def test_training_evidence_never_shows_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent's evidence shows no score for a crashed case, never upstream's floor (SKY-751).

    Args:
        monkeypatch: Stands in for upstream's domain module.
    """
    monkeypatch.setattr(autosaddler_runner, "as_domain", SimpleNamespace(sha256_digest=lambda text: "sha256:" + text))
    store = SimpleNamespace(write_json=lambda path, document, kind: document)
    evaluation = _evaluation({"a": 0.5, "b": autosaddler_runner._NO_SCORE_FLOOR}, frozenset({"b"}))
    evaluation.evaluation_id, evaluation.candidate_id, evaluation.purpose = "e", "c", "train_before"
    for observation in evaluation.observations:
        observation.disposition = "task_failure"

    document = autosaddler_runner.AppEvidenceBuilder(store, {}).build(evaluation)

    assert [record["per_repetition"][0]["score"] for record in document["case_records"]] == [0.5, None]


def test_a_scorer_run_failed_status_with_a_score_is_not_a_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scorer may report the run-failed status with a real score, which counts as a score (SKY-751).

    Args:
        monkeypatch: Stands in for upstream's domain module.
    """
    scored = _evaluation({"a": 0.9, "b": 0.9}, frozenset({"b"}))
    ranking = autosaddler_runner.NoScoreLastRanking(_MeanRanking())

    assert not autosaddler_runner._run_failed(scored)
    assert ranking.select([("scored", scored), ("low", _evaluation({"a": 0.1, "b": 0.1}))])[0] == "scored"
    assert _acceptance(monkeypatch).compare(_evaluation({"a": 0.1, "b": 0.1}), scored).accepted


def test_acceptance_rejects_a_child_with_a_failed_run_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashed child never replaces a fully scored parent; a child that fixes the crash does (SKY-751).

    Args:
        monkeypatch: Stands in for upstream's domain module.
    """
    acceptance = _acceptance(monkeypatch)

    rejected = acceptance.compare(_three(0.3, False), _three(0.9, True))
    fixed = acceptance.compare(_three(0.9, True), _three(0.3, False))

    assert not rejected.accepted
    assert (rejected.before_score, rejected.after_score) == (0.3, 0.9)
    assert rejected.compared_case_ids == ("a", "b", "c")
    assert fixed.accepted
    assert acceptance.compare(_three(0.3, False), _three(0.5, False)).accepted


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (_evaluation({"a": None}, frozenset({"a"})), _evaluation({"a": None}, frozenset({"a"}))),
        (
            _evaluation({"a": None, "b": 0.5}, frozenset({"a"})),
            _evaluation({"a": 0.5, "b": None}, frozenset({"b"})),
        ),
    ],
    ids=["one-case-both-crash", "each-crashes-on-another-case"],
)
def test_acceptance_rejects_when_no_case_is_valid_on_both_versions(
    monkeypatch: pytest.MonkeyPatch, parent: SimpleNamespace, child: SimpleNamespace
) -> None:
    """Both versions failed and no case is valid on both, so the child is rejected, not an upstream error (SKY-751).

    Args:
        monkeypatch: Stands in for upstream's domain module.
        parent: Parent's training evaluation.
        child: Child's training evaluation.
    """
    verdict = _acceptance(monkeypatch).compare(parent, child)

    assert not verdict.accepted
    assert verdict.reason == "no case has a valid score on both versions"
    assert verdict.compared_case_ids == parent.requested_case_ids


def test_acceptance_checks_the_split_and_cases_as_upstream_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper refuses evaluations upstream acceptance refuses, before it records a verdict (SKY-751).

    Args:
        monkeypatch: Stands in for upstream's domain module.
    """
    acceptance = _acceptance(monkeypatch)

    with pytest.raises(ValueError, match="training evaluations only"):
        acceptance.compare(_three(0.3, False, "development"), _three(0.9, True, "development"))
    with pytest.raises(ValueError, match="identical ordered case set"):
        acceptance.compare(_three(0.3, False), _evaluation({"a": 0.9}, frozenset({"a"})))


_HISTORY = ".autosaddler/history"


def _prompt_pack(
    monkeypatch: pytest.MonkeyPatch, history: dict[str, str], no_score: set[str]
) -> autosaddler_runner.AppPromptPack:
    """Build the app prompt pack with upstream's domain, history and assets stood in.

    Args:
        monkeypatch: Stands in for the upstream modules, which need Python 3.12.
        history: The workspace files upstream's history bundle returns.
        no_score: Versions with a development case that has no score.

    Returns:
        The prompt pack the runner uses.
    """
    monkeypatch.setattr(
        autosaddler_runner,
        "as_domain",
        SimpleNamespace(
            canonical_json=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
            sha256_digest=lambda text: f"sha256:{len(text)}",
        ),
    )
    monkeypatch.setattr(autosaddler_runner, "HISTORY_ROOT", _HISTORY)
    monkeypatch.setattr(
        autosaddler_runner, "build_history_bundle", lambda store, context: SimpleNamespace(workspace_files=history)
    )
    monkeypatch.setattr(autosaddler_runner, "SessionSpec", SimpleNamespace)
    monkeypatch.setattr(
        autosaddler_runner,
        "resolved_assets",
        lambda kind, skills, prefix: SimpleNamespace(system_context="", task_prompt="", skills=(), provenance=()),
    )
    return autosaddler_runner.AppPromptPack(
        store=None, component_names=["prompt"], objective=None, background=None, no_score_candidates=no_score
    )


def test_session_context_and_history_never_show_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream's raw means and case scores read as no score, never the floor, in every file the agent reads (SKY-751).

    Args:
        monkeypatch: Stands in for the upstream modules.
    """
    floor = autosaddler_runner._NO_SCORE_FLOOR
    record = {"train_after_aggregate": (0.5 + floor) / 2, "train_after_case_scores": {"a": 0.5, "b": floor}}
    history = {
        f"{_HISTORY}/iterations/0000.json": json.dumps({"iteration": 0, **record}) + "\n",
        f"{_HISTORY}/diffs/x.patch": "-1e300\n",
        f"{_HISTORY}/manifest.json": json.dumps({"files": {}, "through_sequence": 3}) + "\n",
    }
    pack = _prompt_pack(monkeypatch, history, set())

    files = pack.session(
        "reflect", {"candidate_ids": ["c1"], "train_before_aggregate": -0.25, **record}
    ).workspace_files

    context = json.loads(files[autosaddler_runner._SESSION_CONTEXT_PATH])
    iteration = json.loads(files[f"{_HISTORY}/iterations/0000.json"])
    for document in (context, iteration):
        assert document["train_after_aggregate"] is None
        assert document["train_after_case_scores"] == {"a": 0.5, "b": None}
    assert context["train_before_aggregate"] == -0.25
    assert files[f"{_HISTORY}/diffs/x.patch"] == "-1e300\n"
    manifest = json.loads(files[f"{_HISTORY}/manifest.json"])
    assert manifest["through_sequence"] == 3
    assert manifest["files"] == {
        path: {"sha256": f"sha256:{len(text)}", "bytes": len(text.encode("utf-8"))}
        for path, text in files.items()
        if path.startswith(_HISTORY) and not path.endswith("manifest.json")
    }


def test_a_version_with_no_development_score_is_never_offered_as_a_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream accepts on the training batch only; the evolve session never offers a no-score version (SKY-751).

    Args:
        monkeypatch: Stands in for the upstream modules.
    """
    history = {f"{_HISTORY}/manifest.json": json.dumps({"files": {}}) + "\n"}
    context = {"candidate_ids": ["seed", "c1"], "component_source_options": {"x": ["c1"], "y": ["seed", "c1"]}}

    spec = _prompt_pack(monkeypatch, history, {"c1"}).session("evolve", context)

    properties = spec.output_schema["properties"]
    assert properties["parent_ids"]["items"]["enum"] == ["seed"]
    assert properties["component_sources"]["properties"] == {"y": {"type": "string", "enum": ["seed"]}}
    rendered = json.loads(spec.workspace_files[autosaddler_runner._SESSION_CONTEXT_PATH])
    assert rendered["candidate_ids"] == ["seed"]
    assert rendered["component_source_options"] == {"y": ["seed"]}
    every = _prompt_pack(monkeypatch, history, {"seed", "c1"}).session("evolve", context)
    assert every.output_schema["properties"]["parent_ids"]["items"]["enum"] == ["seed", "c1"]


def test_mailbox_reads_a_null_parent_score_as_no_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The parent sends null for a case with no score; the mailbox reads it as negative infinity, not an error (SKY-751).

    Args:
        tmp_path: Working folder with the response file.
        monkeypatch: Working folder and a fixed request id.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(autosaddler_runner.uuid, "uuid4", lambda: SimpleNamespace(hex="r1"))
    (tmp_path / "rpc").mkdir()
    (tmp_path / "rpc" / "r1.json").write_text(json.dumps({"score": None, "info": {"status": "run_failed"}}))

    score, info = autosaddler_runner.EvaluatorMailbox("testnonce", 1.0).evaluate("x", {"id": "a"})

    assert score == float("-inf")
    assert info == {"status": "run_failed"}


_UPSTREAM_POLICIES = """
import autosaddler_runner as runner
from autosaddler.v2.core import domain as d, policies as p


def evaluation(name, scores, split):
    observations = tuple(
        d.Observation.create(
            candidate_id=d.sha256_digest(name), case_id=case, split=split, repetition=0,
            disposition="task_failure", score=runner._NO_SCORE_FLOOR if score is None else score,
            evaluator_fingerprint="f", cost=d.Cost(rollouts=1),
            metadata={"scorer_feedback": {"status": "run_failed"} if score is None else {}},
        )
        for case, score in scores
    )
    return d.Evaluation(
        evaluation_id=d.sha256_digest(name + split), candidate_id=d.sha256_digest(name), split=split,
        purpose="development" if split == "development" else "train_before", iteration=0,
        requested_case_ids=tuple(case for case, _ in scores), observations=observations,
        artifact_dir=d.ArtifactRef(uri="x", kind="k"),
    )


acceptance = runner.NoScoreRejectedAcceptance(p.MatchedValidStrictImprovement())
one = acceptance.compare(evaluation("s", [("a", None)], "train"), evaluation("c", [("a", None)], "train"))
two = acceptance.compare(
    evaluation("s", [("a", None), ("b", 0.5)], "train"), evaluation("c", [("a", 0.5), ("b", None)], "train")
)
better = acceptance.compare(
    evaluation("s", [("a", None), ("b", 0.2)], "train"), evaluation("c", [("a", None), ("b", 0.5)], "train")
)
crashed = acceptance.compare(
    evaluation("s", [("a", 0.2), ("b", 0.2)], "train"), evaluation("c", [("a", None), ("b", 0.9)], "train")
)
ranking = runner.NoScoreLastRanking(p.MeanDevelopmentRanking())
seed = evaluation("s", [("a", None)], "development")
print(one.accepted, one.compared_case_ids, two.accepted, two.compared_case_ids)
print(better.accepted, better.before_score, better.after_score, crashed.accepted, crashed.reason)
print(ranking.select([("seed", seed), ("child", evaluation("c", [("a", None)], "development"))])[0])
print(ranking.select([("seed", seed), ("child", evaluation("c", [("a", 0.4), ("b", None)], "development"))])[0])
scored_seed = evaluation("s", [("a", 0.2), ("b", 0.2)], "development")
print(ranking.select([("seed", scored_seed), ("child", evaluation("c", [("a", 0.9), ("b", None)], "development"))])[0])
"""


def test_upstream_policies_handle_the_no_score_floor(tmp_path: Path) -> None:
    """With upstream's own policies and the floor, crashed versions never win and the search still compares scores (SKY-751).

    Args:
        tmp_path: Folder with the runner files and the probe script.
    """
    python = _upstream_python()
    if python is None:
        pytest.skip(f"set {_PYTHON_ENV} to a Python 3.12 interpreter with the pinned autosaddler package")
    for name, text in native_runtime._runner_files("autosaddler").items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    (tmp_path / "probe.py").write_text(_UPSTREAM_POLICIES)

    run = subprocess.run([python, "probe.py"], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False)

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "False ('a',) False ('a', 'b')",
        "True 0.2 0.5 False the child has a failed [run] entry",
        "seed",
        "seed",
        "seed",
    ]

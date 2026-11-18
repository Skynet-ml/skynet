"""Tests for the AutoSaddler runner: its worker channel and tool loop (SKY-723), and its native box mode.

The pinned upstream package needs Python 3.12 and is only in the worker
image, so most tests cover the parts that do not import it; the end-to-end
native tests skip without an interpreter that carries it. The engine tests in
``test_autosaddler.py`` drive the channel and the tool loop through a real
engine loop.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

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

    def __init__(self, model):
        self.model = model
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
        usage = Usage(input_tokens=5, output_tokens=3, model=self.model)
        return SessionResult(
            status="completed",
            structured_output=output,
            raw_response=canonical_json(output),
            tool_calls=(),
            usage=(usage,),
            cost=Cost(sessions=1, input_tokens=5, output_tokens=3),
        )


# The proposer harness is the only provider path; script it in place of a real harness.
runner.harness_provider = lambda proposer, model, on_refusal=None: ScriptedProvider(model)
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


class _ReplayHost:
    """A fake worker host: it answers every runner request from fixed rules, and records the requests.

    The fake model reads the session's output schema and context with two tool calls, edits the
    repository in a repository run, and then answers with a schema-valid document.
    """

    def __init__(self, files: dict[str, bytes]) -> None:
        """Hold the seed tree's files.

        Args:
            files: The seed tree's file bytes by path.
        """
        self.files = files
        self.requests: list[dict[str, Any]] = []
        self.patches = 0

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        """Answer one request.

        Args:
            request: The runner's request.

        Returns:
            The reply.
        """
        op = request["op"]
        if op == "model":
            return self.model(request["prompt"])
        if op == "command":
            return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False, "tree_id": "after-command"}
        if op == "put_files":
            return {"tree_id": "tree-" + str(len(json.dumps(request, sort_keys=True)))}
        if op == "list_files":
            return {"paths": sorted(self.files)}
        if op == "read_file":
            data = self.files.get(request["path"])
            return {"data": None if data is None else base64.b64encode(data).decode("ascii")}
        if op == "evaluate":
            return {"score": _vowel_fraction(json.dumps(request["candidate"])), "info": {"feedback": "counted"}}
        return {}

    def model(self, prompt: list[dict[str, Any]]) -> dict[str, Any]:
        """Answer one model turn by the session's message count.

        Args:
            prompt: The session's messages.

        Returns:
            Tool calls, or the final document as text.
        """
        if len(prompt) == 2:
            return self.reply(
                [
                    _call("read_file", path=".autosaddler/session_output_schema.json"),
                    _call("read_file", path=".autosaddler/session_context.json"),
                ]
            )
        schema = json.loads(prompt[3]["content"])
        properties = schema["properties"]
        if "changed_paths" in properties and len(prompt) == 5:
            self.patches += 1
            return self.reply(
                [
                    _call("write_file", path="src/app.py", content=f"x = {self.patches + 1}\n"),
                    _call("run_command", command="cat src/app.py"),
                ]
            )
        version = properties["schema_version"]["const"]
        if "changed_paths" in properties:
            output: dict[str, Any] = {"changed_paths": ["src/app.py"]}
        elif "updates" in properties:
            self.patches += 1
            names = properties["updates"]["properties"]
            output = {"updates": dict.fromkeys(names, "bcd" + "a" * (3 * self.patches))}
        elif "parent_ids" in properties:
            parents = properties["parent_ids"]["items"]["enum"]
            output = {"parent_ids": [parents[-1]], "component_sources": {}, "rationale": "continue"}
        else:
            output = {"lessons": []}
        if "changed_paths" in output or "updates" in output:
            output.update({"intent": "improve", "diagnosis": "too low", "expected_effect": "higher"})
        return {"text": json.dumps({"schema_version": version, **output}), "cost_cents": "1", "tool_calls": []}

    @staticmethod
    def reply(calls: list[dict[str, Any]]) -> dict[str, Any]:
        """Build a model reply that asks for tools.

        Args:
            calls: The tool calls.

        Returns:
            The reply.
        """
        return {"text": "", "cost_cents": "1", "tool_calls": calls}


def _run_worker(
    python: str,
    folder: Path,
    payload: dict[str, Any],
    files: dict[str, bytes],
    wrapper: Path | None = None,
    host: _ReplayHost | None = None,
) -> Any:
    """Run the runner in worker mode as the engine does, against a fresh fake host.

    Args:
        python: An interpreter with the pinned upstream package.
        folder: The child's working folder.
        payload: The engine's payload.
        files: The seed tree's files.
        wrapper: A script that loads the runner from its first argument and runs its worker mode, or ``None``
            to run the runner itself.
        host: The fake host, or ``None`` for a ``_ReplayHost`` over ``files``.

    Returns:
        The requests in order, and the result document.
    """
    folder.mkdir()
    host = _ReplayHost(files) if host is None else host
    script = [] if wrapper is None else [str(wrapper)]
    child = subprocess.Popen(
        [python, "-s", "-P", *script, autosaddler_runner.__file__, autosaddler_runner.WORKER_FLAG],
        cwd=folder,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(folder), "LANG": "C.UTF-8", "PYTHONHASHSEED": "0"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    result: dict[str, Any] = {}
    # A hung child would block the read loop for ever; the timer stops it so the test fails instead.
    limit = threading.Timer(120.0, child.kill)
    limit.start()
    try:
        child.stdin.write(json.dumps(payload) + "\n")
        child.stdin.flush()
        for line in child.stdout:
            request = json.loads(line)
            host.requests.append(request)
            if request["op"] == "result":
                result = request["result"]
                break
            child.stdin.write(json.dumps(host.answer(request)) + "\n")
            child.stdin.flush()
        assert child.wait(timeout=60) == 0, child.stderr.read() if child.stderr else ""
    finally:
        limit.cancel()
        if child.poll() is None:
            child.kill()
            child.wait()
    return host.requests, result


@pytest.mark.parametrize("repository", [False, True], ids=["text", "repository"])
def test_a_worker_run_and_its_replay_send_the_same_requests(tmp_path: Path, repository: bool) -> None:
    """Run the worker mode twice with the same replies: the two request streams and results are the same.

    Args:
        tmp_path: Pytest's temporary folder.
        repository: Whether the run optimizes a repository rather than a text.
    """
    python = _upstream_python()
    if python is None:
        pytest.skip(f"set {_PYTHON_ENV} to a Python 3.12 interpreter with the pinned autosaddler package")
    files = {"README.md": b"hi\n", "src/app.py": b"x = 1\n"}
    payload = {
        "engine_id": "autosaddler",
        "task": {
            "name": "autosaddler",
            "seed_candidate": None if repository else "bcd",
            "objective": "maximize vowel density",
            "background": "",
            "train_set": [{"id": 0}, {"id": 1}],
        },
        "max_evals": 12,
        "max_iterations": 2,
        "stop_at_score": None,
        "repo": {"tree_id": "seed", "editable_paths": ["src"], "readonly_paths": [], "constrained": False}
        if repository
        else None,
    }

    first, first_result = _run_worker(python, tmp_path / "first", payload, files)
    second, second_result = _run_worker(python, tmp_path / "second", payload, files)

    assert "error" not in first_result, first_result
    assert first == second
    assert first_result == second_result
    ops = [request["op"] for request in first]
    assert ops.count("model") >= 2
    assert "evaluate" in ops
    if repository:
        assert ops[:2] == ["list_files", "read_file"]
        assert "command" in ops


# Upstream's default session timeout is 30 s; the child lowers it so that a slow model step trips it fast.
_SLOW_STEP_SECONDS = 2.0


class _SlowHost(_ReplayHost):
    """A fake worker host whose first model reply comes later than a short session timeout."""

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        """Answer one request, and wait before the first model reply.

        Args:
            request: The runner's request.

        Returns:
            The reply.
        """
        if request["op"] == "model" and not getattr(self, "waited", False):
            self.waited = True
            time.sleep(_SLOW_STEP_SECONDS)
        return super().answer(request)


_SHORT_TIMEOUT_WORKER = """
import importlib.util, sys

spec = importlib.util.spec_from_file_location("autosaddler_runner", sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ShortTimeoutEngine(runner.AutoSaddlerEngine):
    def __init__(self, *args, diagnosis_patch_timeout_seconds=0.5, **kwargs):
        super().__init__(*args, diagnosis_patch_timeout_seconds=diagnosis_patch_timeout_seconds, **kwargs)


runner.AutoSaddlerEngine = ShortTimeoutEngine
sys.exit(runner.worker_main())
"""


def test_a_slow_model_step_does_not_time_out_its_session(tmp_path: Path) -> None:
    """A model step slower than upstream's default session timeout ends as in a fast run, with no retry.

    A session timeout runs on the real clock, so it would make a live run and its replay differ.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    python = _upstream_python()
    if python is None:
        pytest.skip(f"set {_PYTHON_ENV} to a Python 3.12 interpreter with the pinned autosaddler package")
    script = tmp_path / "short_timeout_worker.py"
    script.write_text(_SHORT_TIMEOUT_WORKER, encoding="utf-8")
    payload = {
        "engine_id": "autosaddler",
        "task": {
            "name": "autosaddler",
            "seed_candidate": "bcd",
            "objective": "maximize vowel density",
            "background": "",
            "train_set": [{"id": 0}, {"id": 1}],
        },
        "max_evals": 12,
        "max_iterations": 1,
        "stop_at_score": None,
        "repo": None,
    }

    fast, fast_result = _run_worker(python, tmp_path / "fast", payload, {}, script)
    slow, slow_result = _run_worker(python, tmp_path / "slow", payload, {}, script, _SlowHost({}))

    assert "error" not in slow_result, slow_result
    assert slow == fast
    assert slow_result == fast_result


def test_the_worker_engine_gets_the_same_long_session_timeouts_in_each_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each session kind gets one fixed timeout far above any model step, so no session times out on the clock.

    Args:
        tmp_path: The process folder.
        monkeypatch: Replaces the upstream names, absent from this interpreter, and the working folder.
    """
    for name in (
        "as_domain",
        "as_ports",
        "as_policies",
        "as_assets",
        "LocalRunStore",
        "ComponentMapHarnessSpace",
        "BaseAgentProvider",
        "WorkspaceRenderer",
        "composition_record",
    ):
        monkeypatch.setattr(autosaddler_runner, name, mock.MagicMock())
    received: list[dict[str, Any]] = []

    class Stop(BaseException):
        """Ends the run once the engine is built."""

    def build(**kwargs: Any) -> Any:
        """Record the engine's arguments and stop.

        Args:
            **kwargs: The engine's arguments.

        Raises:
            Stop: Always.
        """
        received.append(kwargs)
        raise Stop

    monkeypatch.setattr(autosaddler_runner, "AutoSaddlerEngine", build)
    monkeypatch.chdir(tmp_path)
    payload = {
        "engine_id": "autosaddler",
        "task": {"seed_candidate": "bcd", "objective": "", "background": "", "train_set": [{"id": 0}]},
        "max_evals": 2,
        "repo": None,
    }

    for _ in range(2):
        with pytest.raises(Stop):
            autosaddler_runner.execute_worker(payload, _channel()[0])

    timeouts = [
        {key: value for key, value in kwargs.items() if key.endswith("_timeout_seconds")} for kwargs in received
    ]
    assert timeouts[0] == timeouts[1]
    assert sorted(timeouts[0]) == [
        "diagnosis_patch_timeout_seconds",
        "reflection_timeout_seconds",
        "selection_timeout_seconds",
    ]
    assert min(timeouts[0].values()) >= 7 * 24 * 3600


def test_runner_climbs_with_upstream_engine_and_reports_usage(tmp_path: Path) -> None:
    """Run the pinned upstream loop end to end: sessions, parent scoring, acceptance and artifacts.

    Args:
        tmp_path: Pytest's temporary folder.
    """
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
        "proposer": {"harness": "custom"},
        "task": {
            "name": "test",
            "seed_candidate": "bcd",
            "objective": "maximize vowel density",
            "train_set": [{"id": "a"}, {"id": "b"}],
            "val_set": [{"id": "c"}],
        },
    }
    (tmp_path / "input.json").write_text(json.dumps(payload))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONUNBUFFERED": "1"}
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
    # Both upstream splits come from the training examples; the runner does not read ``val_set``.
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


def test_runner_drives_upstream_through_a_configured_harness(tmp_path: Path) -> None:
    """Run the pinned upstream loop with a non-Claude harness rendered into the workspace by upstream.

    Args:
        tmp_path: Pytest's temporary folder.
    """
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
    """Each version is a real checkout the agent edits; the parent scores its patch and the best comes back.

    Args:
        tmp_path: Pytest's temporary folder.
    """
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


def _channel(*replies: dict[str, Any]) -> tuple[autosaddler_runner.WorkerChannel, io.StringIO]:
    """Build a channel whose worker gives fixed replies, and keep what it sends.

    Args:
        *replies: The worker's replies, in order.

    Returns:
        The channel and its request stream.
    """
    writer = io.StringIO()
    reader = io.StringIO("".join(json.dumps(reply) + "\n" for reply in replies))
    return autosaddler_runner.WorkerChannel(reader, writer), writer


def _requests(writer: io.StringIO) -> list[dict[str, Any]]:
    """Read the requests that a channel sent.

    Args:
        writer: The channel's request stream.

    Returns:
        The requests, in order.
    """
    return [json.loads(line) for line in writer.getvalue().splitlines()]


def _plain_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the upstream outcome types, absent from this interpreter, with plain dictionaries.

    Args:
        monkeypatch: The pytest patcher.
    """
    monkeypatch.setattr(autosaddler_runner, "SessionUsage", lambda **fields: fields)
    monkeypatch.setattr(
        autosaddler_runner, "TransportOutcome", lambda raw_response, usage: {"text": raw_response, "usage": usage}
    )


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    """Build one tool call in the engine loop's flat shape.

    Args:
        name: The tool name.
        **arguments: The tool arguments.

    Returns:
        The tool call.
    """
    return {"id": name, "name": name, "arguments": arguments}


def test_a_call_sends_one_sorted_line_and_reads_one_reply() -> None:
    """A request is one JSON line with sorted keys, so its text is the same in a resume."""
    channel, writer = _channel({"text": "hi"})

    assert channel.call("model", tools=None, prompt="p") == {"text": "hi"}
    assert writer.getvalue() == '{"op": "model", "prompt": "p", "tools": null}\n'


def test_a_closed_channel_stops_the_run() -> None:
    """When the worker closes the channel, upstream gets a stop that no retry catches."""
    channel, _ = _channel()

    with pytest.raises(autosaddler_runner.EvaluationStopped):
        channel.call("model", prompt="p", tools=None)


def test_an_evaluation_counts_and_a_spent_budget_stops_every_later_one() -> None:
    """A scored case counts, a missing score is NaN, and a budget stop is final with no new request."""
    channel, writer = _channel(
        {"score": 0.5, "info": {"feedback": "ok"}},
        {"score": None, "info": {}},
        {"error": "spent", "stop_reason": "budget_reached"},
    )

    assert channel.evaluate("a", {"id": 0}, 3) == (0.5, {"feedback": "ok"})
    assert math.isnan(channel.evaluate("b")[0])
    with pytest.raises(autosaddler_runner.BudgetStopped, match="spent"):
        channel.evaluate("c")
    with pytest.raises(autosaddler_runner.BudgetStopped):
        channel.evaluate("d")

    assert channel.total_evals == 2
    assert [request["candidate"] for request in _requests(writer)] == ["a", "b", "c"]
    assert _requests(writer)[0] == {"op": "evaluate", "candidate": "a", "example": {"id": 0}, "candidate_id": 3}


def test_an_evaluation_error_that_is_no_budget_stop_is_a_plain_stop() -> None:
    """Any other refusal stops the run, but not as a spent budget."""
    channel, _ = _channel({"error": "metric broke"})

    with pytest.raises(autosaddler_runner.EvaluationStopped) as caught:
        channel.evaluate("a")

    assert type(caught.value) is autosaddler_runner.EvaluationStopped


def test_files_go_to_the_tree_store_as_base64() -> None:
    """Tree writes and reads carry bytes as base64, and a refused write is a value error."""
    channel, writer = _channel({"tree_id": "t2"}, {"data": "aGk="}, {"data": None}, {"error": "ValueError: bad"})

    assert channel.put_files({"b": b"\x00", "a": b"hi"}, "t1") == "t2"
    assert channel.read_file("t2", "a") == b"hi"
    assert channel.read_file("t2", "c") is None
    with pytest.raises(ValueError, match="bad"):
        channel.put_files({"..": b""}, None)

    assert _requests(writer)[0] == {"op": "put_files", "files": {"a": "aGk=", "b": "AA=="}, "base_tree_id": "t1"}
    assert _requests(writer)[1] == {"op": "read_file", "tree_id": "t2", "path": "a"}


def test_scrub_hides_the_process_folder_and_clock_times(tmp_path: Path) -> None:
    """A prompt names no folder or time that differs between a run and its resume.

    Args:
        tmp_path: The process folder that the prompt must not name.
    """
    text = f"see {tmp_path}/a.txt at 2026-11-17T10:20:30.5+00:00 and 2026-11-17 10:20:30Z"

    assert autosaddler_runner.scrub(text, tmp_path) == "see /work/a.txt at <time> and <time>"


def test_seeded_ids_are_the_same_in_each_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run and its resume make the same ``uuid4`` ids and random values.

    Args:
        monkeypatch: Restores ``uuid.uuid4`` after the test.
    """
    monkeypatch.setattr(uuid, "uuid4", uuid.uuid4)
    autosaddler_runner.seed_ids()
    first = [uuid.uuid4(), uuid.uuid4(), random.random()]
    autosaddler_runner.seed_ids()
    second = [uuid.uuid4(), uuid.uuid4(), random.random()]

    assert first == second
    assert first[0] != first[1]
    assert first[0].version == 4


def test_a_module_that_imported_uuid4_by_name_gets_the_seeded_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream's engine does ``from uuid import uuid4`` before the runner seeds the ids; it gets them too.

    Args:
        monkeypatch: Restores ``uuid.uuid4`` and removes the stand-in module after the test.
    """
    monkeypatch.setattr(uuid, "uuid4", uuid.uuid4)
    module = ModuleType("upstream_engine_stand_in")
    module.uuid4 = uuid.uuid4  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    autosaddler_runner.seed_ids()
    first = module.uuid4()  # type: ignore[attr-defined]
    autosaddler_runner.seed_ids()

    assert module.uuid4() == first  # type: ignore[attr-defined]


def test_upstream_modules_get_a_counted_clock_and_others_keep_the_real_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream's event times, wall seconds and git commit times are the same in a run and its resume.

    Args:
        monkeypatch: Adds the stand-in modules and the git date variables, and removes them after the test.
    """

    def read(module: ModuleType) -> list[object]:
        """Read two event times and two wall clock values from a stand-in module.

        Args:
            module: The stand-in module.

        Returns:
            The values in the order they were read.
        """
        return [
            module.datetime.now(UTC).isoformat(),  # type: ignore[attr-defined]
            module.time.monotonic(),  # type: ignore[attr-defined]
            module.datetime.now(UTC).isoformat(),  # type: ignore[attr-defined]
            module.time.monotonic(),  # type: ignore[attr-defined]
        ]

    monkeypatch.delenv("GIT_AUTHOR_DATE", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_DATE", raising=False)
    runs = []
    for _ in range(2):
        upstream = ModuleType("autosaddler.v2.core.stand_in")
        other = ModuleType("not_autosaddler")
        for module in (upstream, other):
            module.time = time  # type: ignore[attr-defined]
            module.datetime = datetime  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, module.__name__, module)
        autosaddler_runner.fix_clocks()
        runs.append(read(upstream))
        assert other.time is time  # type: ignore[attr-defined]
        assert other.datetime is datetime  # type: ignore[attr-defined]

    assert runs[0] == runs[1]
    assert runs[0] == ["2000-01-01T00:00:00+00:00", 1.0, "2000-01-01T00:00:02+00:00", 3.0]
    assert os.environ["GIT_AUTHOR_DATE"] == os.environ["GIT_COMMITTER_DATE"] == "2000-01-01T00:00:00+00:00"


def test_upstream_modules_get_a_counted_clock_for_every_clock_they_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wall time, counters, ns clocks, ``utcnow``, ``today`` and a bound ``datetime`` module all count.

    Args:
        monkeypatch: Adds the stand-in modules and the git date variables, and removes them after the test.
    """
    datetime_module = sys.modules["datetime"]

    def read(module: ModuleType) -> list[object]:
        """Read each clock once from a stand-in module.

        Args:
            module: The stand-in module.

        Returns:
            The values in the order they were read.
        """
        names: Any = module
        return [
            names.time.time(),
            names.time.perf_counter(),
            names.time.time_ns(),
            names.time.monotonic_ns(),
            names.time.perf_counter_ns(),
            names.datetime.utcnow().isoformat(),
            names.datetime.today().isoformat(),
            names.datetime_module.datetime.now(UTC).isoformat(),
            names.monotonic(),
        ]

    monkeypatch.delenv("GIT_AUTHOR_DATE", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_DATE", raising=False)
    runs = []
    for _ in range(2):
        upstream = ModuleType("autosaddler.v2.core.stand_in")
        names: Any = upstream
        names.time, names.datetime, names.datetime_module, names.monotonic = (
            time,
            datetime,
            datetime_module,
            time.monotonic,
        )
        monkeypatch.setitem(sys.modules, upstream.__name__, upstream)
        autosaddler_runner.fix_clocks()
        runs.append(read(upstream))

    start = datetime(2000, 1, 1, tzinfo=UTC).timestamp()
    assert runs[0] == runs[1]
    assert runs[0] == [
        start,
        1.0,
        int((start + 2) * 1e9),
        3_000_000_000,
        4_000_000_000,
        "2000-01-01T00:00:05",
        "2000-01-01T00:00:06",
        "2000-01-01T00:00:07+00:00",
        8.0,
    ]
    assert time.monotonic() > 8.0


def test_the_real_upstream_modules_hold_no_real_clock_after_the_patch(tmp_path: Path) -> None:
    """After ``fix_clocks``, no pinned upstream module holds the real ``time``, ``datetime`` or a clock function.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    python = _upstream_python()
    if python is None:
        pytest.skip(f"set {_PYTHON_ENV} to a Python 3.12 interpreter with the pinned autosaddler package")
    probe = (
        "import datetime, importlib.util, sys, time\n"
        "spec = importlib.util.spec_from_file_location('autosaddler_runner', sys.argv[1])\n"
        "runner = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(runner)\n"
        "runner.fix_clocks()\n"
        "real = (time, datetime, datetime.datetime, time.time, time.monotonic, time.perf_counter, time.time_ns)\n"
        "held = sorted(\n"
        "    f'{name}.{key}' for name, module in sys.modules.items() if name.split('.')[0] == 'autosaddler'\n"
        "    for key, value in vars(module).items() if any(value is clock for clock in real)\n"
        ")\n"
        "assert not held, held\n"
    )
    script = tmp_path / "probe.py"
    script.write_text(probe, encoding="utf-8")

    done = subprocess.run(
        [python, "-s", "-P", str(script), autosaddler_runner.__file__],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "LANG": "C.UTF-8", "PYTHONHASHSEED": "0"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr


def test_tool_arguments_reach_the_next_prompt_with_sorted_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live reply and its replay from the step log can order the argument keys differently; the prompt is the same.

    Args:
        tmp_path: The process folder.
        monkeypatch: Replaces the upstream outcome types.
    """
    _plain_outcomes(monkeypatch)
    prompts = []
    for arguments in ({"path": "a.txt", "content": "x"}, {"content": "x", "path": "a.txt"}):
        workspace = tmp_path / str(len(prompts))
        workspace.mkdir()
        channel, writer = _channel(
            {"text": "", "cost_cents": "1", "tool_calls": [{"id": "c1", "name": "write_file", "arguments": arguments}]},
            {"text": "done", "cost_cents": "1", "tool_calls": []},
        )
        autosaddler_runner.StepTransport(channel, tmp_path, None, None).converse(workspace, "go")
        prompts.append(_requests(writer)[1]["prompt"])

    assert prompts[0] == prompts[1]
    assert prompts[0][2]["tool_calls"][0]["function"]["arguments"] == '{"content": "x", "path": "a.txt"}'


@pytest.mark.parametrize("arguments", ["{bad", "[1]", [1], None])
def test_tool_arguments_that_are_no_json_object_reach_the_next_prompt_as_an_empty_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: Any
) -> None:
    """A provider adapter that parses earlier tool arguments must not fail on raw text in the history.

    Args:
        tmp_path: The process folder.
        monkeypatch: Replaces the upstream outcome types.
        arguments: The raw text of arguments that are no JSON object, or a value that is no object.
    """
    _plain_outcomes(monkeypatch)
    channel, writer = _channel(
        {"text": "", "cost_cents": "1", "tool_calls": [{"id": "c1", "name": "list_files", "arguments": arguments}]},
        {"text": "done", "cost_cents": "1", "tool_calls": []},
    )

    autosaddler_runner.StepTransport(channel, tmp_path, None, None).converse(tmp_path, "go")

    prompt = _requests(writer)[1]["prompt"]
    assert prompt[2]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert prompt[3]["content"].startswith("Error: TypeError")


def test_long_tool_output_says_what_was_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long file read keeps its start and says how much is cut; a long command output keeps its end.

    Args:
        tmp_path: The process folder.
        monkeypatch: Replaces the upstream outcome types and the output limit.
    """
    _plain_outcomes(monkeypatch)
    monkeypatch.setattr(autosaddler_runner, "_TOOL_OUTPUT_CHARS", 10)
    workspace = tmp_path / "session"
    workspace.mkdir()
    (workspace / "big.txt").write_text("0123456789abcdef")
    channel, writer = _channel(
        {"text": "", "cost_cents": "1", "tool_calls": [_call("read_file", path="big.txt")]},
        {"text": "done", "cost_cents": "1", "tool_calls": []},
    )

    autosaddler_runner.StepTransport(channel, tmp_path, None, None).converse(workspace, "go")

    content = _requests(writer)[1]["prompt"][-1]["content"]
    assert content.startswith("0123456789\n")
    assert "6 more characters were cut" in content
    assert autosaddler_runner.clip("run_command", "0123456789abcdef") == (
        "[The first 6 characters were cut.]\n6789abcdef"
    )
    assert autosaddler_runner.clip("read_file", "short") == "short"


def test_a_file_read_keeps_its_clock_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file read shows the file's date-times as they are, so a write back does not corrupt the file.

    A command output still hides its clock times, because they differ between a run and its resume.

    Args:
        tmp_path: The process folder.
        monkeypatch: Replaces the upstream outcome types.
    """
    _plain_outcomes(monkeypatch)
    workspace = tmp_path / "session"
    workspace.mkdir()
    text = "Create Date: 2026-11-17 10:20:30.123456\n"
    (workspace / "rev.py").write_text(text)
    channel, writer = _channel(
        {
            "text": "",
            "cost_cents": "1",
            "tool_calls": [_call("read_file", path="rev.py"), _call("run_command", command="date")],
        },
        {"tree_id": "t1"},
        {"exit_code": 0, "stdout": "2026-11-18T01:02:03Z", "stderr": "", "timed_out": False, "tree_id": "t2"},
        {"text": "done", "cost_cents": "1", "tool_calls": []},
    )

    autosaddler_runner.StepTransport(channel, tmp_path, None, None).converse(workspace, "go")

    read, command = _requests(writer)[-1]["prompt"][-2:]
    assert read["content"] == text
    assert command["content"] == "exit code 0\nstdout:\n<time>\nstderr:\n"


def test_a_session_runs_tools_until_the_model_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool loop sends each tool result back, and a command runs on a tree of the workspace.

    Args:
        tmp_path: The process folder.
        monkeypatch: Replaces the upstream outcome types.
    """
    _plain_outcomes(monkeypatch)
    workspace = tmp_path / "session"
    (workspace / ".autosaddler").mkdir(parents=True)
    (workspace / ".autosaddler" / "INSTRUCTIONS.md").write_text(f"Work in {workspace}.")
    channel, writer = _channel(
        {"text": "", "cost_cents": "1", "tool_calls": [_call("write_file", path="a.txt", content="x")]},
        {"text": "", "cost_cents": "2", "tool_calls": [_call("run_command", command="cat a.txt")]},
        {"tree_id": "t1"},
        {"exit_code": 0, "stdout": "x", "stderr": "", "timed_out": False, "tree_id": "t2"},
        {"text": "done", "cost_cents": "3", "tool_calls": []},
    )

    outcome = autosaddler_runner.StepTransport(channel, tmp_path, None, None).converse(workspace, "go")

    assert outcome["text"] == "done"
    assert outcome["usage"][0]["provider_cost"] == 0.06
    assert outcome["usage"][0]["provider_cost_unit"] == "usd"
    assert outcome["usage"][0]["status"] == "success"
    assert (workspace / "a.txt").read_text() == "x"
    requests = _requests(writer)
    assert [request["op"] for request in requests] == ["model", "model", "put_files", "command", "model"]
    assert requests[0]["prompt"][0] == {"role": "system", "content": "Work in /work/session."}
    assert requests[0]["tools"] == autosaddler_runner._TOOLS
    assert sorted(requests[2]["files"]) == [".autosaddler/INSTRUCTIONS.md", "a.txt"]
    assert requests[3] == {"op": "command", "tree_id": "t1", "command": "cat a.txt"}
    assert requests[4]["prompt"][-1] == {
        "role": "tool",
        "tool_call_id": "run_command",
        "content": "exit code 0\nstdout:\nx\nstderr:\n",
    }


def test_a_failed_model_step_or_the_turn_limit_fails_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed model failure, or a model that never stops calling tools, ends the session as failed.

    Args:
        tmp_path: The process folder.
        monkeypatch: Replaces the upstream outcome types and the turn limit.
    """
    _plain_outcomes(monkeypatch)
    monkeypatch.setattr(autosaddler_runner, "_MAX_TURNS", 2)
    failed, _ = _channel({"error": "ModelCallFailedError: ConnectionError"})
    looping, _ = _channel(*[{"text": "", "cost_cents": "1", "tool_calls": [_call("list_files")]}] * 2)

    first = autosaddler_runner.StepTransport(failed, tmp_path, None, None).converse(tmp_path, "go")
    second = autosaddler_runner.StepTransport(looping, tmp_path, None, None).converse(tmp_path, "go")

    assert "ConnectionError" in first["text"]
    assert first["usage"][0]["status"] == "failed"
    assert first["usage"][0]["provider_cost"] is None
    assert first["usage"][0]["provider_cost_unit"] is None
    assert "limit of 2" in second["text"]
    assert second["usage"][0]["status"] == "failed"


def test_a_failed_model_step_keeps_its_committed_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A billed model failure adds its cost to the session's cost before the session fails.

    Args:
        tmp_path: The process folder.
        monkeypatch: Replaces the upstream outcome types.
    """
    _plain_outcomes(monkeypatch)
    channel, _ = _channel(
        {"text": "", "cost_cents": "1", "tool_calls": [_call("list_files")]},
        {"error": "ModelCallFailedError: ConnectionError", "cost_cents": "3"},
    )

    outcome = autosaddler_runner.StepTransport(channel, tmp_path, None, None).converse(tmp_path, "go")

    assert outcome["usage"][0]["status"] == "failed"
    assert outcome["usage"][0]["provider_cost"] == pytest.approx(0.04)
    assert outcome["usage"][0]["provider_cost_unit"] == "usd"


@pytest.mark.parametrize("path", ["../out.txt", "/etc/passwd/../../x/../../..", ".git/config", "a/../../b"])
def test_a_tool_path_outside_the_workspace_is_refused(tmp_path: Path, path: str) -> None:
    """The model gets an error text for a path that leaves the workspace or enters ``.git``.

    Args:
        tmp_path: The process folder.
        path: The model's path.
    """
    workspace = tmp_path / "session"
    workspace.mkdir()
    channel, writer = _channel()
    transport = autosaddler_runner.StepTransport(channel, tmp_path, None, None)

    output = transport.tool(workspace, _call("write_file", path=path, content="x"))

    assert output.startswith("Error: ValueError")
    assert writer.getvalue() == ""
    assert sorted(tmp_path.rglob("*")) == [workspace]


@pytest.mark.parametrize("arguments", ["[]", '"x"', "1", "null", "{bad", [1]])
def test_tool_arguments_that_are_no_json_object_give_the_model_an_error(tmp_path: Path, arguments: Any) -> None:
    """The model gets an error text, and the session goes on, when its tool arguments are no JSON object.

    Args:
        tmp_path: The session workspace.
        arguments: The raw text of arguments that are no JSON object, as the flat tool call keeps it, or a value.
    """
    channel, writer = _channel()
    transport = autosaddler_runner.StepTransport(channel, tmp_path, None, None)

    output = transport.tool(tmp_path, {"id": "c1", "name": "list_files", "arguments": arguments})

    assert output.startswith("Error: TypeError"), output
    assert writer.getvalue() == ""


def test_a_work_path_reads_as_workspace_relative(tmp_path: Path) -> None:
    """The model sees ``/work``, so a path under it is a workspace path.

    Args:
        tmp_path: The session workspace.
    """
    assert autosaddler_runner._inside(tmp_path, "/work/a/b.txt") == tmp_path.resolve() / "a" / "b.txt"


def test_a_repository_command_sends_only_the_changed_files(tmp_path: Path) -> None:
    """In a repository run the box starts from the seed tree, so only changed and new files go up.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    workspace = tmp_path / "session"
    workspace.mkdir()
    (workspace / "same.txt").write_text("same")
    (workspace / "edit.txt").write_text("old")
    autosaddler_runner._git_output(workspace, "init", "--quiet")
    autosaddler_runner._git_output(workspace, "add", "--all")
    autosaddler_runner._git_output(workspace, "commit", "--quiet", "-m", "base")
    base = autosaddler_runner._git_output(workspace, "rev-parse", "HEAD").strip()
    (workspace / "edit.txt").write_text("new")
    (workspace / "new.txt").write_text("added")
    channel, _ = _channel()

    files = autosaddler_runner.StepTransport(channel, tmp_path, "seed", base).workspace_files(workspace)

    assert files == {"edit.txt": b"new", "new.txt": b"added"}


def test_a_repository_command_removes_the_files_the_version_deleted(tmp_path: Path) -> None:
    """The box starts from the seed tree, so the command first removes what the version deleted or moved.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    workspace = tmp_path / "session"
    workspace.mkdir()
    for name in ("keep.txt", "gone.txt", "old name.txt"):
        (workspace / name).write_text(name)
    autosaddler_runner._git_output(workspace, "init", "--quiet")
    autosaddler_runner._git_output(workspace, "add", "--all")
    autosaddler_runner._git_output(workspace, "commit", "--quiet", "-m", "base")
    base = autosaddler_runner._git_output(workspace, "rev-parse", "HEAD").strip()
    (workspace / "gone.txt").unlink()
    (workspace / "old name.txt").rename(workspace / "moved.txt")
    # A staged move reads as a rename to git, unless the diff turns renames off.
    autosaddler_runner._git_output(workspace, "add", "--all")
    channel, writer = _channel(
        {"tree_id": "t1"}, {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False, "tree_id": "t2"}
    )

    autosaddler_runner.StepTransport(channel, tmp_path, "seed", base).tool(
        workspace, _call("run_command", command="ls")
    )

    put, command = _requests(writer)
    assert sorted(put["files"]) == ["moved.txt"]
    assert command["command"] == "rm -f -- gone.txt 'old name.txt'\nls"


def test_a_repository_command_with_no_deletion_runs_as_given(tmp_path: Path) -> None:
    """With nothing deleted, the command goes to the box unchanged.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    workspace = tmp_path / "session"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a")
    autosaddler_runner._git_output(workspace, "init", "--quiet")
    autosaddler_runner._git_output(workspace, "add", "--all")
    autosaddler_runner._git_output(workspace, "commit", "--quiet", "-m", "base")
    base = autosaddler_runner._git_output(workspace, "rev-parse", "HEAD").strip()
    channel, writer = _channel(
        {"tree_id": "t1"}, {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False, "tree_id": "t2"}
    )

    autosaddler_runner.StepTransport(channel, tmp_path, "seed", base).tool(
        workspace, _call("run_command", command="ls")
    )

    assert _requests(writer)[1]["command"] == "ls"


def test_the_seed_tree_becomes_a_git_checkout(tmp_path: Path) -> None:
    """One listing call and one read per file rebuild the seed tree, with no box command.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    channel, writer = _channel({"paths": ["run.sh", "src/a.py"]}, {"data": "ZWNobyBoaQo="}, {"data": "cHJpbnQoMSkK"})

    base = autosaddler_runner.checkout_seed(channel, "seed", tmp_path / "checkout")

    checkout = tmp_path / "checkout"
    assert (checkout / "src" / "a.py").read_text() == "print(1)\n"
    assert (checkout / "run.sh").read_text() == "echo hi\n"
    assert autosaddler_runner._git_output(checkout, "rev-parse", "HEAD").strip() == base
    assert autosaddler_runner._git_output(checkout, "status", "--porcelain") == ""
    assert _requests(writer) == [
        {"op": "list_files", "tree_id": "seed", "path": ""},
        {"op": "read_file", "tree_id": "seed", "path": "run.sh"},
        {"op": "read_file", "tree_id": "seed", "path": "src/a.py"},
    ]


def test_a_seed_tree_leaves_out_its_git_folder(tmp_path: Path) -> None:
    """Seeded Git config and hooks are not read or written, so the checkout's own ``git`` calls run none of them.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    marker = tmp_path / "ran"
    files = {
        ".git/config": f"[core]\n\tfsmonitor = touch {marker}\n".encode(),
        ".git/hooks/post-commit": f"#!/bin/sh\ntouch {marker}\n".encode(),
        "src/.GIT/config": b"[core]\n",
        "a.txt": b"a\n",
    }
    read: list[str] = []

    def read_file(tree_id: str, path: str) -> bytes:
        """Serve one seed file, and note which one was read.

        Args:
            tree_id: The seed tree.
            path: The file path.

        Returns:
            The file bytes.
        """
        read.append(path)
        return files[path]

    channel = SimpleNamespace(list_files=lambda tree_id, path: list(files), read_file=read_file)

    base = autosaddler_runner.checkout_seed(channel, "seed", tmp_path / "checkout")

    checkout = tmp_path / "checkout"
    assert read == ["a.txt"]
    assert (checkout / "a.txt").read_text() == "a\n"
    assert not (checkout / "src").exists()
    assert (checkout / ".git" / "config").read_bytes() != files[".git/config"]
    assert not (checkout / ".git" / "hooks" / "post-commit").exists()
    assert not marker.exists()
    assert autosaddler_runner._git_output(checkout, "rev-parse", "HEAD").strip() == base


def test_a_seed_tree_the_worker_can_not_list_stops_the_run(tmp_path: Path) -> None:
    """When the seed tree is no folder the worker can list, the run has no versions to start from.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    channel, _ = _channel({"paths": None})

    with pytest.raises(RuntimeError, match="Could not list the seed tree"):
        autosaddler_runner.checkout_seed(channel, "seed", tmp_path / "checkout")


def test_worker_events_go_out_as_progress_and_log_requests() -> None:
    """The evaluator's box prefixes become the worker's ``progress`` and ``log`` requests."""
    channel, writer = _channel({}, {})

    channel.emit(autosaddler_runner._PROGRESS_PREFIX, {"event": "case_scored", "score": math.inf})
    channel.emit(autosaddler_runner._LOG_PREFIX, {"event": "rule.block"})

    assert _requests(writer) == [
        {"op": "progress", "event": {"event": "case_scored", "score": None}},
        {"op": "log", "record": {"event": "rule.block"}},
    ]


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a constraints file, a path block reads the fixed message and reaches the run log as ``rule.block``.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces upstream's verdict type.
    """
    channel, writer = _channel({})
    accepted, check, reason = _verify_readme_edit(tmp_path, monkeypatch, autosaddler_runner.rule_blocker(channel))
    assert (accepted, check, reason) == (False, "repository-rules", repo_tree.ILLEGAL_ACTION_MESSAGE)
    requests = _requests(writer)
    assert [request["op"] for request in requests] == ["log"]
    record = requests[0]["record"]
    assert record["event"] == repo_tree.RULE_BLOCK_EVENT
    assert record["source"] == "engine"
    assert record["fields"]["rule"] == repo_tree.PATH_RULE
    assert "README.md" in record["fields"]["reason"]


def test_a_constrained_repository_block_in_a_box_prints_a_run_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under a constraints file, a path block reads the fixed message and reaches the run log as ``rule.block``.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces upstream's verdict type.
        capsys: Captures the run event line.
    """
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a constraints file, the verifier names the refused path and logs no rule event.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces upstream's verdict type.
    """
    accepted, check, reason = _verify_readme_edit(tmp_path, monkeypatch, None)
    assert (accepted, check) == (False, "repository-rules")
    assert "README.md" in reason


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


@pytest.mark.parametrize(("cost_usd", "cost", "unit"), [(0.25, 0.25, "usd"), (0.0, None, None)])
def test_a_harness_session_cost_carries_its_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cost_usd: float, cost: float | None, unit: str | None
) -> None:
    """Upstream refuses a cost with no unit, so a harness session's dollar cost goes out as ``usd``.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces the upstream outcome types, the harness session and ``HOME``.
        cost_usd: The harness session's dollar cost.
        cost: The provider cost the session must report.
        unit: The cost unit the session must report.
    """
    _plain_outcomes(monkeypatch)
    outcome = autosaddler_runner.harness_bridge.SessionOutcome(
        text="done",
        stdout="",
        stderr="",
        returncode=0,
        usage={"input_tokens": 5, "output_tokens": 3},
        cost_usd=cost_usd,
        duration_seconds=1.0,
    )
    monkeypatch.setattr(autosaddler_runner.harness_bridge, "run_session", lambda *args, **kwargs: outcome)
    monkeypatch.setenv("HOME", str(tmp_path))
    transport = autosaddler_runner.HarnessTransport({}, "m1")
    session = SimpleNamespace(workspace=tmp_path, task_prompt="go", session_id="s1")

    (usage,) = asyncio.run(transport.run(session, 1.0))["usage"]

    assert (usage["provider_cost"], usage["provider_cost_unit"]) == (cost, unit)


def test_harness_sessions_send_each_refusal_to_the_run_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pass each hidden-path refusal of a harness session to the run log as ``proposer.refusal``.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Replaces the harness session and ``HOME``.
        capsys: Captures the run log line.
    """
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

"""Verify held-out cases never reach a repository run's proposer during the search (SKY-649).

Each test marks the held-out cases' inputs and expected values with unique
strings, runs a repository job through the service, and greps everything the
proposer box and the search-phase run log receive for those strings.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from core.constants import PROGRESS_EVALUATION_STARTED
from core.models.blackbox import BlackboxRunRequest
from core.models.common import SplitFractions

from ...unified.split import CaseSplits, split_cases
from .. import native_runtime, repo_tree
from .. import service as service_mod
from ..harness import GatewayConfig
from ..sandbox import _CODEX_REQUIREMENTS_READBACK, CODEX_REQUIREMENTS_PATH, CommandResult, SandboxSpec
from .mocks import FakeReflectionLM

HELDOUT_INPUT = "HELDOUT_INPUT_c41d7e"
HELDOUT_EXPECTED = "HELDOUT_EXPECTED_9b02fa"
MARKERS = (HELDOUT_INPUT, HELDOUT_EXPECTED)
_PATCH = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
_RUNNER_MARK = "native_input.json"


def _cases() -> list[dict[str, Any]]:
    """Build eight cases; the last two carry the held-out markers.

    Returns:
        Six plain cases, then two cases whose input and expected value hold a marker.
    """
    plain = [{"input": {"q": f"train {n}"}, "expected": f"answer {n}"} for n in range(6)]
    held = [{"input": {"q": f"{HELDOUT_INPUT} {n}"}, "expected": f"{HELDOUT_EXPECTED} {n}"} for n in range(2)]
    return plain + held


def _splits() -> CaseSplits:
    """Split the cases in order so the two marked cases are the held-out test split.

    Returns:
        Four train, two validation and two held-out cases.
    """
    splits = split_cases(_cases(), "pairs", SplitFractions(train=0.5, val=0.25, test=0.25), shuffle=False, seed=0)
    assert [HELDOUT_INPUT in json.dumps(case) for case in splits.test] == [True, True]
    assert not any(marker in json.dumps([*splits.train, *splits.val]) for marker in MARKERS)
    return splits


def _is_heldout(case: Any) -> bool:
    """Report whether a case is one of the marked held-out cases.

    Args:
        case: A case the scorer received.

    Returns:
        True when the case holds a marker.
    """
    return any(marker in json.dumps(case, default=str) for marker in MARKERS)


def _assert_no_marker(label: str, text: str) -> None:
    """Fail when ``text`` holds a held-out marker.

    Args:
        label: What the text is, for the failure message.
        text: Text the proposer or the search-phase run log can see.
    """
    for marker in MARKERS:
        assert marker not in text, f"held-out marker {marker} reached {label}"


class _Scorer:
    """Score every case the same and record which cases arrive before and after the search."""

    usage = None

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[tuple[Any, Any]] = []
        self.search_done = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Record the call and return a fixed score.

        Args:
            candidate: The version to score.
            case: The case to score it on.

        Returns:
            A score that improves on any patch, with short feedback.
        """
        with self.lock:
            self.calls.append((self.search_done.is_set(), case))
        return (0.75 if candidate else 0.5), {"feedback": "scored"}

    def close(self) -> None:
        """Release nothing; the service calls this when the run ends."""


class _BoxSession:
    """A proposer box backed by a private directory that records every file and command.

    In scripted mode, the runner command is a scripted child that asks the
    parent to score each train case it was given. In real mode, the real
    runner runs in the directory as a subprocess with a fake agent.
    """

    def __init__(self, root: Path, *, real: bool, agent_bin: Path | None = None) -> None:
        """Create the box directory.

        Args:
            root: The box directory.
            real: Whether the runner command runs the real runner.
            agent_bin: A directory to put first on ``PATH`` in real mode.
        """
        self.root = root
        self.root.mkdir(parents=True)
        self.real = real
        self.agent_bin = agent_bin
        self.writes: list[tuple[str, str]] = []
        self.commands: list[tuple[str, dict[str, str]]] = []
        self.output: list[str] = []
        self.closed = False

    def write_files(self, files: dict[str, str]) -> None:
        """Write files into the box and keep a copy of each write.

        Args:
            files: Box-relative paths and their text.
        """
        for name, text in files.items():
            self.writes.append((name, text))
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def read_file(self, path: str) -> str | None:
        """Read one box file.

        Args:
            path: Box-relative path.

        Returns:
            The text, or ``None`` when the file does not exist.
        """
        target = self.root / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Run the bootstrap and the Codex setup as no-ops, and the runner command as scripted or for real.

        Args:
            command: The bootstrap or the runner command.
            **kwargs: Timeout, environment and output callback.

        Returns:
            The process outcome.
        """
        env = dict(kwargs.get("env") or {})
        self.commands.append((command, env))
        if CODEX_REQUIREMENTS_PATH in command:
            return CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK)
        if _RUNNER_MARK not in command:
            (self.root / "native-python.txt").write_text(sys.executable, encoding="utf-8")
            return CommandResult(exit_code=0)
        if self.real:
            return self._run_real(command, env, kwargs["on_output"])
        return self._run_scripted(kwargs["on_output"])

    def _run_scripted(self, sink: Any) -> CommandResult:
        """Ask the parent to score a patch on each train case the box got, within its budget, then write a result.

        Args:
            sink: The parent's output callback.

        Returns:
            A successful process outcome.
        """
        payload = json.loads((self.root / _RUNNER_MARK).read_text(encoding="utf-8"))
        nonce = payload["nonce"]
        score = None
        examples = payload["task"]["train_set"][: payload["max_evals"]]
        for index, example in enumerate(examples):
            request_id = f"{index:032x}"
            line = f"APP_NATIVE_RPC {nonce} {json.dumps({'id': request_id, 'candidate': _PATCH, 'example': example})}\n"
            self.output.append(line)
            sink("stdout", line)
            answer = json.loads((self.root / "rpc" / f"{request_id}.json").read_text(encoding="utf-8"))
            score = answer.get("score")
            progress = {"candidate_id": 0, "candidate": _PATCH, "score": score, "total_evals": index + 1}
            sink("stdout", f"APP_NATIVE_PROGRESS {nonce} {json.dumps(progress)}\n")
        result = {
            "best_candidate": _PATCH,
            "best_score": score,
            "total_evals": len(examples),
            "metadata": {},
            "usage_by_model": {"fake/model": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            "usage_complete": True,
        }
        (self.root / "native_result.json").write_text(json.dumps(result), encoding="utf-8")
        return CommandResult(exit_code=0)

    def _run_real(self, command: str, env: dict[str, str], sink: Any) -> CommandResult:
        """Run the real runner command in the box and stream its output to the parent.

        Args:
            command: The runner command the service built.
            env: The environment the service set for it.
            sink: The parent's output callback.

        Returns:
            The process outcome.
        """
        process_env = {
            **env,
            "PATH": f"{self.agent_bin}{os.pathsep}/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        process = subprocess.Popen(
            ["bash", "-c", command],
            cwd=self.root,
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stderr: list[str] = []
        reader = threading.Thread(target=lambda: stderr.extend(process.stderr), daemon=True)
        reader.start()
        try:
            for line in process.stdout:
                self.output.append(line)
                sink("stdout", line)
            code = process.wait(timeout=120)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        reader.join(timeout=5)
        return CommandResult(exit_code=code, stderr="".join(stderr))

    def close(self) -> None:
        """Record that the service closed the box."""
        self.closed = True


class _BoxRuntime:
    """Open one recording box for each native engine the run starts."""

    injects_headers = True

    def __init__(self, root: Path, *, real: bool = False, agent_bin: Path | None = None) -> None:
        """Keep where to put the boxes.

        Args:
            root: Parent directory of every box.
            real: Whether the boxes run the real runner.
            agent_bin: The fake agent's directory, for real mode.
        """
        self.root = root
        self.real = real
        self.agent_bin = agent_bin
        self.sessions: list[_BoxSession] = []
        self.specs: list[SandboxSpec] = []

    def open(self, spec: SandboxSpec) -> _BoxSession:
        """Open a new box.

        Args:
            spec: The sandbox the service asked for.

        Returns:
            The recording box.
        """
        self.specs.append(spec)
        session = _BoxSession(self.root / f"box-{len(self.sessions)}", real=self.real, agent_bin=self.agent_bin)
        self.sessions.append(session)
        return session


def _repo_snapshot(tmp_path: Path) -> dict[str, Any]:
    """Pack a one-file repository into upload chunks, as the parent stages it.

    Args:
        tmp_path: Where to put the tree, its archive and its chunks.

    Returns:
        The snapshot block the service passes to the native runtime.
    """
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text("x = 1\n")
    archive = tmp_path / "tree.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    chunks = []
    for index, chunk in enumerate(repo_tree.archive_chunks(archive)):
        chunks.append(tmp_path / f"tree.{index:04d}.b64")
        chunks[-1].write_text(chunk, encoding="ascii")
    return {"chunks": [str(chunk) for chunk in chunks], "readonly_paths": [], "constrained": False}


def _request(strategy: dict[str, Any], **overrides: Any) -> BlackboxRunRequest:
    """Build a repository run request.

    Args:
        strategy: The run's strategy block.
        **overrides: Further request fields.

    Returns:
        The validated request.
    """
    data: dict[str, Any] = {
        "recipe": "repo",
        "scorer": {"kind": "python", "metric_code": "def score(repo_path):\n    return 1.0\n"},
        "reflection_model_config": {"name": "fake/model"},
        "budget": {"max_scorer_runs": 12},
        "strategy": strategy,
        "max_cost_cents": 100,
        "target": {"kind": "repo", "repo": {"repository": "octo/hello", "editable_paths": ["src"]}},
        **overrides,
    }
    return BlackboxRunRequest.model_validate(data)


def _run(
    request: BlackboxRunRequest,
    runtime: _BoxRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> tuple[Any, _Scorer, list[tuple[str, dict[str, Any]]], list[logging.LogRecord]]:
    """Run a repository job through the service with the given box runtime.

    Args:
        request: The repository run request.
        runtime: The recording box runtime.
        tmp_path: Per-test directory.
        monkeypatch: Replaces the model, gateway, runtime and pinned source.
        caplog: Captures the run log.

    Returns:
        The response, the scorer, every progress event, and the log records made before the final run.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    monkeypatch.setattr(service_mod, "build_language_model", lambda config, *, disable_cache=False: FakeReflectionLM())
    monkeypatch.setattr(
        service_mod, "gateway_from_settings", lambda _settings: GatewayConfig("https://unused.example/v1", "test-key")
    )
    monkeypatch.setattr(service_mod, "current_sandbox_runtime", lambda: runtime)
    caplog.set_level(logging.DEBUG)
    scorer = _Scorer()
    events: list[tuple[str, dict[str, Any]]] = []
    search_records: list[logging.LogRecord] = []

    def progress(event: str, metrics: dict[str, Any]) -> None:
        """Record each event and mark where the search ends."""
        if event == PROGRESS_EVALUATION_STARTED and not scorer.search_done.is_set():
            search_records.extend(caplog.records)
            scorer.search_done.set()
        events.append((event, dict(metrics)))

    response = service_mod.run_job(
        service_mod.job_spec_from_payload(request),
        scorer,
        _splits(),
        artifact_id="sky-649",
        started=time.perf_counter(),
        progress_callback=progress,
        gepa_log_dir_path=str(tmp_path / "run"),
        repo_snapshot=_repo_snapshot(tmp_path),
    )
    assert scorer.search_done.is_set()
    return response, scorer, events, search_records


def _assert_search_is_clean(
    runtime: _BoxRuntime,
    scorer: _Scorer,
    events: list[tuple[str, dict[str, Any]]],
    search_records: list[logging.LogRecord],
) -> None:
    """Assert no held-out marker reached the boxes or the search-phase run data, and the final run used them.

    Args:
        runtime: The recording box runtime.
        scorer: The run's scorer.
        events: Every progress event of the run.
        search_records: Log records made before the final run.
    """
    assert runtime.sessions
    # The train cases do reach the box, so the scan below reads where case data lands.
    assert any("train 0" in text for session in runtime.sessions for _, text in session.writes)
    for index, session in enumerate(runtime.sessions):
        for name, text in session.writes:
            _assert_no_marker(f"box {index} file {name}", text)
        for path in session.root.rglob("*"):
            if path.is_file():
                _assert_no_marker(f"box {index} file {path}", path.read_text(encoding="utf-8", errors="replace"))
        for command, env in session.commands:
            _assert_no_marker(f"box {index} command", command)
            _assert_no_marker(f"box {index} env", json.dumps(env))
        _assert_no_marker(f"box {index} output", "".join(session.output))
    for spec in runtime.specs:
        _assert_no_marker("sandbox spec", repr(spec))
    search_events = events[: next(i for i, (e, _) in enumerate(events) if e == PROGRESS_EVALUATION_STARTED)]
    assert search_events
    _assert_no_marker("search progress events", json.dumps(search_events, default=str))
    for record in search_records:
        _assert_no_marker("search run log", record.getMessage() + json.dumps(record.__dict__, default=str))
    search_cases = [case for after, case in scorer.calls if not after]
    assert search_cases
    assert not any(_is_heldout(case) for case in search_cases)
    assert sum(_is_heldout(case) for after, case in scorer.calls if after) >= 2


@pytest.mark.parametrize(
    "strategy",
    [
        {"mode": "single", "engine": "gepa"},
        {"mode": "single", "engine": "best_of_n"},
        {"mode": "single", "engine": "meta_harness"},
        {"mode": "single", "engine": "autoresearch"},
        {"mode": "single", "engine": "autosaddler"},
        {"mode": "single", "engine": "shinka_evolve"},
        {"mode": "auto"},
    ],
    ids=lambda strategy: strategy.get("engine", "auto"),
)
def test_heldout_cases_never_enter_any_engine_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, strategy: dict[str, Any]
) -> None:
    """Every engine's box, its scoring answers and the search-phase run data hold no held-out case.

    Args:
        tmp_path: Per-test directory for the boxes and the repository.
        monkeypatch: Replaces the model, gateway, runtime and pinned source.
        caplog: Captures the run log.
        strategy: The engine or Auto.
    """
    runtime = _BoxRuntime(tmp_path / "boxes")

    response, scorer, events, search_records = _run(_request(strategy), runtime, tmp_path, monkeypatch, caplog)

    _assert_search_is_clean(runtime, scorer, events, search_records)
    assert response.split_counts.test == 2
    assert response.case_count == 2


def test_the_check_fails_when_a_heldout_case_enters_the_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Put one held-out case into the box's task file and confirm the check catches it.

    Args:
        tmp_path: Per-test directory for the boxes and the repository.
        monkeypatch: Replaces the box task document, the model, gateway, runtime and pinned source.
        caplog: Captures the run log.
    """
    real_payload = native_runtime.native_task_payload

    def leaky_payload(engine_id: str, task: Any) -> dict[str, Any]:
        """Add the first held-out case to the box's train set, as a leak would."""
        document = real_payload(engine_id, task)
        document["train_set"] = [*document["train_set"], {"id": 0, "input": _cases()[-2]["input"]}]
        return document

    monkeypatch.setattr(native_runtime, "native_task_payload", leaky_payload)
    runtime = _BoxRuntime(tmp_path / "boxes")

    _, scorer, events, search_records = _run(
        _request({"mode": "single", "engine": "gepa"}), runtime, tmp_path, monkeypatch, caplog
    )

    with pytest.raises(AssertionError, match=f"held-out marker {HELDOUT_INPUT} reached box 0 file native_input.json"):
        _assert_search_is_clean(runtime, scorer, events, search_records)


_AGENT = """\
import json, os, pathlib, subprocess, sys
record = pathlib.Path(sys.argv[1])
record.mkdir(exist_ok=True)
seen = {}
for root in {pathlib.Path(os.environ["HOME"]), pathlib.Path.cwd()}:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 4_000_000:
            seen[str(path)] = path.read_text(encoding="utf-8", errors="replace")
prompt = pathlib.Path(os.environ["APP_PROMPT_FILE"]).read_text(encoding="utf-8")
document = {"cwd": os.getcwd(), "prompt": prompt, "env": dict(os.environ), "files": seen}
(record / f"session-{len(list(record.iterdir()))}.json").write_text(json.dumps(document))
here = pathlib.Path.cwd()
if (here / "save.sh").exists():
    (here / "repo/src/app.py").write_text("x = 2\\n")
    subprocess.run(["./save.sh", "better"], check=True, capture_output=True)
    (here / "logs/run/pending_eval.json").write_text(
        json.dumps({"candidates": [{"name": "better", "file": "agents/better.patch"}]})
    )
elif (here / "eval.sh").exists():
    (here / "repo/src/app.py").write_text("x = 2\\n")
    subprocess.run(["./eval.sh"], check=True, capture_output=True)
else:
    (here / "src/app.py").write_text("x = 2\\n")
print("done")
"""


@pytest.mark.parametrize("engine", ["gepa", "best_of_n", "meta_harness", "autoresearch"])
def test_heldout_cases_never_reach_the_real_runner_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, engine: str
) -> None:
    """The real box runner's agent never sees a held-out case in its prompt, environment or workspace.

    The agent is a script behind the custom harness. Each session saves its
    prompt, its environment and the text of every file it can reach.

    Args:
        tmp_path: Per-test directory for the box, the agent and its records.
        monkeypatch: Replaces the model, gateway, runtime and pinned source.
        caplog: Captures the run log.
        engine: A native engine whose upstream package this machine has.
    """
    agent = tmp_path / "agent" / "agent.py"
    agent.parent.mkdir()
    agent.write_text(_AGENT, encoding="utf-8")
    record = tmp_path / "record"
    runtime = _BoxRuntime(tmp_path / "boxes", real=True, agent_bin=tmp_path / "agent")
    budget = {"max_scorer_runs": 16, **({"max_iterations": 1} if engine == "meta_harness" else {})}
    request = _request(
        {"mode": "single", "engine": engine},
        budget=budget,
        proposer={"harness": "custom", "run_command": f'"{sys.executable}" "{agent}" "{record}"', "ralph": False},
    )

    response, scorer, events, search_records = _run(request, runtime, tmp_path, monkeypatch, caplog)

    _assert_search_is_clean(runtime, scorer, events, search_records)
    sessions = [json.loads(path.read_text()) for path in sorted(record.iterdir())]
    assert sessions, "the agent never ran"
    # The agent does see train case data, so its view is where a held-out case would show.
    assert any("train 0" in json.dumps(session) for session in sessions)
    for index, session in enumerate(sessions):
        _assert_no_marker(f"agent session {index} prompt", session["prompt"])
        _assert_no_marker(f"agent session {index} env", json.dumps(session["env"]))
        assert session["files"]
        for path, text in session["files"].items():
            _assert_no_marker(f"agent session {index} file {path}", text)
    assert response.split_counts.test == 2

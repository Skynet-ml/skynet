"""Verify upstream native-process transport, isolation and usage reconciliation."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
import threading
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.task import Task as UpstreamTask

from core.billing.pricing import model_token_costs
from core.billing.runtime import UsagePendingError
from core.billing.usage_tags import USAGE_TAGS_HEADER
from core.billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from core.constants import PROGRESS_CANDIDATE, PROGRESS_CASE_SCORED
from core.exceptions import ServiceError
from core.models.blackbox import BlackboxProposer, BlackboxTarget
from core.service_gateway.language_models import total_tokens_from_history, usage_by_model_from_history
from core.service_gateway.optimization.cost_ceiling import CostCeilingExceededError

from .. import (
    autosaddler_runner,
    best_of_n,
    gepa_engine,
    harness_bridge,
    native_engines,
    native_runner,
    native_runtime,
    repo_tree,
    sandbox_log,
)
from ..harness import GatewayConfig, build_launch, launch_payload
from ..native_runtime import NativeOptions, _bootstrap_command, check_native_runtime, run_native_engine
from ..protocol import EvalServer, Result, ScorerAbortError, Task
from ..sandbox import CommandResult, SandboxSpec


class FakeSession:
    """Run a scripted child dialogue without provisioning infrastructure."""

    def __init__(self, *, timeout: bool = False, incomplete_usage: bool = False) -> None:
        """Set the desired execution outcome.

        Args:
            timeout: Whether the child command reaches its deadline.
            incomplete_usage: Whether proposer usage remains unreconciled.
        """
        self.files: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.timeout = timeout
        self.incomplete_usage = incomplete_usage
        self.no_incumbent = False
        self.candidate: Any = "better"

    def write_files(self, files: dict[str, str]) -> None:
        """Persist files as an in-memory child filesystem.

        Args:
            files: Relative paths and text contents.
        """
        self.files.update(files)

    def read_file(self, path: str) -> str | None:
        """Read one child file.

        Args:
            path: Child-relative path.

        Returns:
            Stored text, or ``None``.
        """
        return self.files.get(path)

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Emit fragmented and repeated requests during the execution command.

        Args:
            command: Installation or runner command.
            **kwargs: Runtime timeout, environment and output callback.

        Returns:
            The scripted process outcome.
        """
        self.calls.append((command, kwargs))
        if len(self.calls) == 1:
            return CommandResult(exit_code=0)
        payload = json.loads(self.files["native_input.json"])
        request_id = "a" * 32
        request = {"id": request_id, "candidate": self.candidate, "example": {"id": "case"}}
        line = f"APP_NATIVE_RPC {payload['nonce']} {json.dumps(request)}\n"
        sink = kwargs["on_output"]
        sink("stdout", "unrelated upstream log\n" + line[:17])
        sink("stdout", line[17:])
        sink("stdout", line)
        response = json.loads(self.files[f"rpc/{request_id}.json"])
        if "score" in response:
            progress = {"candidate_id": 0, "candidate": self.candidate, "score": response["score"], "total_evals": 1}
            sink("stdout", f"APP_NATIVE_PROGRESS {payload['nonce']} {json.dumps(progress)}\n")
        document = {
            "best_candidate": self.candidate,
            "best_score": response.get("score"),
            "total_evals": 1,
            "metadata": {"adapter_cost": 0.01},
            "usage_by_model": {"claude-test": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}},
            "usage_complete": not self.incomplete_usage,
        }
        if "error" in response:
            document["error"] = response["error"]
        if self.no_incumbent:
            document["best_candidate"] = payload["task"]["seed_candidate"] or ""
            document["best_score"] = None
        self.files["native_result.json"] = json.dumps(document)
        return CommandResult(exit_code=0, timed_out=self.timeout)

    def close(self) -> None:
        """Record that the runtime was destroyed."""
        self.closed = True


class FakeRuntime:
    """Expose a scripted process session through the real runtime protocol."""

    injects_headers = True

    def __init__(self, session: FakeSession) -> None:
        """Retain the session supplied by a test.

        Args:
            session: Fake process filesystem and dialogue.
        """
        self.session = session
        self.spec: SandboxSpec | None = None

    def open(self, spec: SandboxSpec) -> FakeSession:
        """Capture runtime creation arguments.

        Args:
            spec: Process environment, identity and gateway injection.

        Returns:
            The scripted session.
        """
        self.spec = spec
        return self.session


class ReadinessSession(FakeSession):
    """Report dependency checks without executing an optimizer or an evaluator."""

    def __init__(self, *, fail: bool = False, pending: bool = False, bootstrap: CommandResult | None = None) -> None:
        """Select a runtime readiness failure, a scripted bootstrap result or unresolved final sandbox usage."""
        super().__init__()
        self.fail = fail
        self.pending = pending
        self.bootstrap = bootstrap

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Capture offline runtime probes and return the required explicit success marker."""
        self.calls.append((command, kwargs))
        if self.bootstrap is not None and len(self.calls) == 1:
            return self.bootstrap
        return CommandResult(
            exit_code=1 if self.fail else 0, stdout='{"ready": true, "autosaddler": true, "shinka": true}\n'
        )

    def close(self) -> None:
        """Always record closure while preserving an unresolved sandbox usage signal."""
        self.closed = True
        if self.pending:
            raise UsagePendingError("readiness usage pending")


def test_native_readiness_checks_selected_isolation_without_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe the managed native image without model or scorer requests.

    Args:
        monkeypatch: Pytest fixture for replacing the pinned source archive.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "verified-source")
    session = ReadinessSession()
    adapter = FakeRuntime(session)
    options = NativeOptions(
        runtime="vercel",
        model="test/model",
        gateway=GatewayConfig(url="http://127.0.0.1:9000/v1", api_key="scoped"),
        budget_route={"url": "http://127.0.0.1:9000/v1", "token": "scoped"},
        sandbox_runtime=adapter,
        max_token_cost=1,
    )
    assert check_native_runtime(options) == {
        "runtime": "vercel",
        "gepa_source": native_runtime.GEPA_SOURCE,
        "meta_harness_source": native_runtime.META_HARNESS_REVISION,
        "autoresearch_version": native_runtime.AUTORESEARCH_VERSION,
        "autosaddler_source": native_runtime.AUTOSADDLER_REVISION,
        "autosaddler_ready": True,
        "shinka_version": native_runtime.SHINKA_VERSION,
        "shinka_ready": True,
    }
    assert adapter.spec.network_disabled is True
    assert "native_input.json" not in session.files
    assert "native_result.json" not in session.files
    assert session.files["native_source.tar.gz.b64"] == "verified-source"
    commands = "\n".join(command for command, _kwargs in session.calls)
    assert "npm install" not in commands
    assert "pip install" not in commands
    assert "bwrap_prefix" not in commands
    assert "engine.run" not in commands
    assert "--print" not in commands
    assert "scoped" not in str(session.calls)
    assert session.closed


@pytest.mark.parametrize("pending", [False, True])
def test_native_readiness_does_not_hide_failure_or_pending_usage(
    monkeypatch: pytest.MonkeyPatch, pending: bool
) -> None:
    """Fail readiness for missing dependencies and keep unresolved usage pending."""
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "verified-source")
    session = ReadinessSession(fail=not pending, pending=pending)
    options = NativeOptions(
        runtime="vercel",
        model="test/model",
        gateway=GatewayConfig(url="http://127.0.0.1:9000/v1", api_key="scoped"),
        budget_route={"url": "http://127.0.0.1:9000/v1", "token": "scoped"},
        sandbox_runtime=FakeRuntime(session),
        max_token_cost=1,
    )
    with pytest.raises(UsagePendingError if pending else ServiceError):
        check_native_runtime(options)
    assert session.closed


@pytest.mark.parametrize(
    ("bootstrap", "detail"),
    [
        (
            CommandResult(
                exit_code=1,
                stderr='Traceback (most recent call last):\n  File "<string>", line 1, in <module>\n'
                "AssertionError: Native optimizers need Python 3.11 or newer\n",
            ),
            "The check exited with status 1: AssertionError: Native optimizers need Python 3.11 or newer",
        ),
        (CommandResult(exit_code=124, timed_out=True), "The check timed out after 60s."),
        (CommandResult(exit_code=2), "The check exited with status 2 and no output."),
    ],
)
def test_native_readiness_failure_names_the_broken_step(
    monkeypatch: pytest.MonkeyPatch, bootstrap: CommandResult, detail: str
) -> None:
    """Surface the failing bootstrap step instead of a bare dependency verdict.

    Args:
        monkeypatch: Pytest fixture for replacing the pinned source archive.
        bootstrap: Scripted result of the offline dependency bootstrap.
        detail: Expected explanation appended to the readiness error.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "verified-source")
    session = ReadinessSession(bootstrap=bootstrap)
    options = NativeOptions(
        runtime="vercel",
        model="test/model",
        gateway=GatewayConfig(url="http://127.0.0.1:9000/v1", api_key="scoped"),
        budget_route={"url": "http://127.0.0.1:9000/v1", "token": "scoped"},
        sandbox_runtime=FakeRuntime(session),
        max_token_cost=1,
    )
    with pytest.raises(ServiceError) as failure:
        check_native_runtime(options)
    assert str(failure.value) == f"The selected native runtime lacks its required pinned offline dependencies. {detail}"
    assert len(session.calls) == 1
    assert session.closed


def test_native_python_floor_matches_pyproject_and_admits_the_host() -> None:
    """Keep the offline bootstrap floor at requires-python so an image built for this host passes it."""
    pyproject = tomllib.loads((Path(__file__).resolve().parents[5] / "pyproject.toml").read_text())
    requires = pyproject["project"]["requires-python"]
    assert requires.startswith(">=")
    assert tuple(int(part) for part in requires[2:].split(".")) == native_runtime.PYTHON_FLOOR
    assert sys.version_info >= native_runtime.PYTHON_FLOOR
    protected = _bootstrap_command("vercel", protected=True)
    assert f"sys.version_info >= {native_runtime.PYTHON_FLOOR!r}" in protected
    assert "3, 11, 8" not in protected
    assert "3, 11, 8" not in _bootstrap_command("vercel")


def _context(tmp_path: Path, runtime: FakeRuntime, kind: str = "vercel") -> SimpleNamespace:
    """Build only the engine context fields this transport needs.

    Args:
        tmp_path: Artifact destination.
        runtime: Injected deterministic runtime.
        kind: Runtime selection sent to the child.

    Returns:
        An engine-compatible context with native options.
    """
    return SimpleNamespace(
        native_options=NativeOptions(
            kind, "claude-test", GatewayConfig("https://gateway.example/v1", "secret"), 1.0, sandbox_runtime=runtime
        ),
        run_dir=str(tmp_path),
        concurrency=2,
        max_iterations=1,
        stop_at_score=None,
    )


def test_native_transport_preserves_engine_choice_and_scores_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The managed sandbox preserves engine identity and deduplicates paid scoring.

    Args:
        tmp_path: Artifact destination.
        monkeypatch: Pytest fixture for replacing the pinned source archive.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    runtime = FakeRuntime(session)
    calls: list[tuple[str, Any]] = []

    def score(candidate: str, example: Any) -> tuple[float, dict[str, Any]]:
        """Record the actual evaluator call."""
        calls.append((candidate, example))
        return 0.75, {"feedback": "improved"}

    ctx = _context(tmp_path, runtime)
    progress: list[dict[str, Any]] = []
    ctx.progress_callback = lambda event, metrics: progress.append(metrics)
    result = run_native_engine(
        "meta_harness", Task("seed", cases=[{"id": "case"}]), EvalServer(score, max_evals=3), ctx
    )

    assert calls == [("better", {"id": "case"})]
    assert result.best_candidate == "better"
    assert result.best_score == 0.75
    assert json.loads(session.files["native_input.json"])["sandbox"] is False
    assert "test_set" not in json.loads(session.files["native_input.json"])["task"]
    assert "secret" not in session.files["native_input.json"]
    assert session.calls[1][1]["env"]["ANTHROPIC_AUTH_TOKEN"] == "app-managed"
    assert runtime.spec.inject_headers == {"gateway.example": {"Authorization": "Bearer secret"}}
    assert ctx.native_options.usage_by_model["claude-test"]["total_tokens"] == 13
    assert progress[0]["score"] == 0.75
    assert progress[0]["parent_id"] is None
    assert session.closed


def test_protected_managed_runtime_does_not_nest_upstream_jail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the already isolated outer sandbox as the native engine boundary.

    Args:
        tmp_path: Artifact destination inside the outer sandbox.
        monkeypatch: Pinned source fixture.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    runtime = FakeRuntime(session)
    runtime.protected = True
    runtime.injects_headers = False
    run_native_engine(
        "meta_harness",
        Task("seed", cases=[{"id": "case"}]),
        EvalServer(lambda *_: (0.75, {}), max_evals=3),
        _context(tmp_path, runtime),
    )
    assert json.loads(session.files["native_input.json"])["sandbox"] is False
    assert session.closed


@pytest.mark.parametrize("engine_id", ["autoresearch", "gepa_repo", "best_of_n_repo", "meta_harness", "autosaddler"])
def test_repository_tree_and_rules_reach_every_repository_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_id: str
) -> None:
    """Upload the packed tree chunk by chunk with its rules; the guest never gets registry network."""
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    chunks = [tmp_path / "tree.0000.b64", tmp_path / "tree.0001.b64"]
    for index, chunk in enumerate(chunks):
        chunk.write_text(f"chunk{index}")
    session = FakeSession()
    session.candidate = ""
    ctx = _context(tmp_path, FakeRuntime(session))
    repo = {
        "chunks": [str(chunk) for chunk in chunks],
        "editable_paths": ["src"],
        "readonly_paths": ["vendor/lib"],
        "constrained": True,
    }
    ctx.native_options = replace(ctx.native_options, repo=repo)
    task = Task("", cases=[{"id": "a"}, {"id": "b"}])
    run_native_engine(engine_id, task, EvalServer(lambda *_: (0.5, {}), max_evals=3), ctx)

    assert ctx.native_options.sandbox_runtime.spec.allowed_hosts == ()
    assert not set(ctx.native_options.sandbox_runtime.spec.allowed_hosts) & set(PACKAGE_REGISTRY_HOSTS)
    assert (session.files["repo-tree/tree.0000.b64"], session.files["repo-tree/tree.0001.b64"]) == ("chunk0", "chunk1")
    assert json.loads(session.files["native_input.json"])["repo"] == {
        "chunks": ["repo-tree/tree.0000.b64", "repo-tree/tree.0001.b64"],
        "editable_paths": ["src"],
        "readonly_paths": ["vendor/lib"],
        "constrained": True,
    }
    assert session.files["repo_tree.py"] == Path(repo_tree.__file__).read_text()


@pytest.mark.parametrize("engine_id", ["gepa_repo", "best_of_n_repo"])
def test_repository_only_engines_refuse_a_text_run(tmp_path: Path, engine_id: str) -> None:
    """GEPA's and Best-of-N's agent samplers edit a checkout, so a text run never reaches them."""
    ctx = _context(tmp_path, FakeRuntime(FakeSession()))
    with pytest.raises(ServiceError, match="needs a repository checkout"):
        run_native_engine(engine_id, Task(""), EvalServer(lambda *_: (0.5, {}), max_evals=3), ctx)


def test_gepa_hands_a_repository_run_to_the_native_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GEPA on a repository runs in the native sandbox, with the coding agent as its proposer."""
    calls: list[str] = []

    def fake_native(engine_id: str, *_: object) -> Result:
        """Record which native engine GEPA asked for."""
        calls.append(engine_id)
        return Result(best_candidate="", best_score=1.0, total_evals=1)

    monkeypatch.setattr(gepa_engine, "run_native_engine", fake_native)
    ctx = _context(tmp_path, FakeRuntime(FakeSession()))
    ctx.native_options = replace(ctx.native_options, repo={"chunks": [], "editable_paths": ["."], "readonly_paths": []})
    result = gepa_engine.GepaEngine().run(Task(""), EvalServer(lambda *_: (0.5, {}), max_evals=3), ctx)
    assert calls == ["gepa_repo"]
    assert result.best_score == 1.0


def test_best_of_n_hands_a_repository_run_to_the_native_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-of-N on a repository samples coding-agent versions in the native sandbox."""
    calls: list[str] = []

    def fake_native(engine_id: str, *_: object) -> Result:
        """Record which native engine Best-of-N asked for."""
        calls.append(engine_id)
        return Result(best_candidate="", best_score=1.0, total_evals=1)

    monkeypatch.setattr(best_of_n, "run_native_engine", fake_native)
    ctx = _context(tmp_path, FakeRuntime(FakeSession()))
    ctx.native_options = replace(ctx.native_options, repo={"chunks": [], "editable_paths": ["."], "readonly_paths": []})
    result = best_of_n.BestOfNEngine().run(Task(""), EvalServer(lambda *_: (0.5, {}), max_evals=3), ctx)
    assert calls == ["best_of_n_repo"]
    assert result.best_score == 1.0


def test_gepa_agent_proposer_needs_a_repository(tmp_path: Path) -> None:
    """Text runs keep GEPA's reflection model; the agent proposer exists only for checkouts."""
    ctx = _context(tmp_path, FakeRuntime(FakeSession()))
    with pytest.raises(ServiceError, match="needs a repository checkout"):
        run_native_engine("gepa_repo", Task(""), EvalServer(lambda *_: (0.5, {}), max_evals=3), ctx)


_AUTORESEARCH_AGENT = (
    "pathlib.Path('repo/src/a.py').write_text('a = 2\\n')\n"
    "done=subprocess.run(['./eval.sh'],capture_output=True,text=True)\n"
    "assert done.returncode==0, done.stdout+done.stderr\n"
)
# GEPA's agent works inside the checkout itself and never scores its own edit.
_GEPA_AGENT = "pathlib.Path('src/a.py').write_text('a = 2\\n')\n"


@pytest.mark.parametrize(
    ("engine_id", "agent", "starts"),
    [
        ("autoresearch", _AUTORESEARCH_AGENT, []),
        ("gepa_repo", _GEPA_AGENT, [[]]),
        ("best_of_n_repo", _GEPA_AGENT, []),
    ],
)
def test_real_native_runner_unpacks_the_repository_for_its_engine(
    tmp_path: Path, engine_id: str, agent: str, starts: list[list[str]]
) -> None:
    """The runner rebuilds the checkout, and the agent's edit reaches the parent as a diff.

    GEPA scores the unedited commit first; AutoResearch scores only what its agent submits.
    """
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "a.py").write_text("a = 1\n")
    archive = tmp_path / "tree.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    (tmp_path / "repo-tree").mkdir()
    chunk_names = []
    for index, chunk in enumerate(repo_tree.archive_chunks(archive)):
        chunk_names.append(f"repo-tree/tree.{index:04d}.b64")
        (tmp_path / chunk_names[-1]).write_text(chunk)
    binary = tmp_path / "bin"
    binary.mkdir()
    fake_cli = binary / "claude"
    fake_cli.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, subprocess, sys\n"
        "args=sys.argv[1:]; session=args[args.index('--session-id')+1]\n"
        + agent
        + "print(json.dumps({'total_cost_usd':0.01,'session_id':session,"
        "'modelUsage':{'claude-test':{'inputTokens':7,'outputTokens':3}}}))\n"
    )
    fake_cli.chmod(0o755)
    (tmp_path / "rpc").mkdir()
    payload = {
        "nonce": "testnonce",
        "engine_id": engine_id,
        "model": "claude-test",
        "sandbox": False,
        "max_token_cost": 0.05,
        "max_evals": 4,
        "max_concurrency": 1,
        "timeout_seconds": 20,
        "task": {"name": "test", "seed_candidate": ""},
        "proposer": {"ralph": False},
        "repo": {"chunks": chunk_names, "editable_paths": ["src"], "readonly_paths": []},
    }
    (tmp_path / "input.json").write_text(json.dumps(payload))
    env = {"PATH": f"{binary}{os.pathsep}/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(
        [sys.executable, native_runner.__file__, "input.json"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    candidates = []
    try:
        for line in process.stdout:
            if line.startswith("APP_NATIVE_RPC testnonce "):
                request = json.loads(line.split(" ", 2)[2])
                candidates.append(request["candidate"])
                response = {"score": 0.9 if "+a = 2" in request["candidate"] else 0.5, "info": {}}
                (tmp_path / "rpc" / f"{request['id']}.json").write_text(json.dumps(response))
        stderr = process.stderr.read()
        assert process.wait(timeout=25) == 0, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    scored = [repo_tree.patch_paths(candidate) for candidate in candidates]
    assert scored[: len(starts)] == starts
    assert ["src/a.py"] in scored
    result = json.loads((tmp_path / "native_result.json").read_text())
    assert repo_tree.patch_paths(result["best_candidate"]) == ["src/a.py"]
    assert "+a = 2" in result["best_candidate"]
    assert result["best_score"] == 0.9


def test_parent_scorer_abort_survives_child_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The original parent failure propagates after available usage is retained and teardown runs."""
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    ctx = _context(tmp_path, FakeRuntime(session))
    error = ScorerAbortError("dataset service is unavailable")

    def score(candidate: str, example: Any) -> tuple[float, dict[str, Any]]:
        """Reject evaluation with the exact exception the caller must receive."""
        raise error

    with pytest.raises(ScorerAbortError) as raised:
        run_native_engine("autoresearch", Task("seed"), EvalServer(score, max_evals=3), ctx)
    assert raised.value is error
    assert ctx.native_options.usage_by_model["claude-test"]["total_tokens"] == 13
    assert session.closed


@pytest.mark.parametrize(
    ("timeout", "incomplete", "match"), [(True, False, "runtime limit"), (False, True, "reconciled")]
)
def test_native_timeout_and_missing_usage_fail_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: bool, incomplete: bool, match: str
) -> None:
    """A failed native execution never switches to a different local algorithm."""
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession(timeout=timeout, incomplete_usage=incomplete)
    ctx = _context(tmp_path, FakeRuntime(session))
    with pytest.raises(ServiceError, match=match):
        run_native_engine("autoresearch", Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=2), ctx)
    assert len(session.calls) == 2
    assert session.closed


def test_native_run_lives_as_long_as_its_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run has no wall-clock cap of its own; only the sandbox's platform lifetime bounds it."""
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    monkeypatch.setattr(native_runtime.settings, "vercel_sandbox_max_lifetime_seconds", 86_400.0)
    session = FakeSession()
    runtime = FakeRuntime(session)
    run_native_engine(
        "autoresearch", Task("seed"), EvalServer(lambda *_: (1.0, {}), max_evals=2), _context(tmp_path, runtime)
    )
    assert runtime.spec is not None
    assert runtime.spec.lifetime_seconds == 86_400.0
    assert session.calls[-1][1]["timeout_seconds"] > 85_000


@pytest.mark.parametrize("runner", [native_runner, autosaddler_runner])
def test_supervisor_stop_is_not_reported_as_a_reply_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: Any
) -> None:
    """A run stopped by its supervisor keeps that reason instead of a fake evaluator timeout."""
    monkeypatch.chdir(tmp_path)
    mailbox = runner.EvaluatorMailbox("nonce", 60.0)
    threading.Timer(0.2, mailbox.stopped.set).start()
    with pytest.raises(runner.EvaluationStopped, match="stopped this run"):
        mailbox.evaluate("candidate")
    assert mailbox.error is None


@pytest.mark.parametrize(("sent", "expected"), [(None, float("-inf")), (0.5, 0.5)])
def test_box_mailbox_turns_a_null_parent_score_into_no_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: float | None, expected: float
) -> None:
    """A metric error reaches the box as null and the upstream engine sees its own no-score value (SKY-367)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rpc").mkdir()
    mailbox = native_runner.EvaluatorMailbox("nonce", 5.0)

    def reply(prefix: str, payload: dict[str, Any]) -> None:
        """Answer the request at once, as the parent does.

        Args:
            prefix: Event family, ignored.
            payload: The evaluation request.
        """
        (tmp_path / "rpc" / f"{payload['id']}.json").write_text(json.dumps({"score": sent, "info": {"x": 1}}))

    monkeypatch.setattr(mailbox, "emit", reply)

    assert mailbox.evaluate("candidate") == (expected, {"x": 1})


def test_native_usage_ledger_survives_lane_replacement() -> None:
    """Auto lanes share cumulative proposer usage when their cost caps differ."""
    options = NativeOptions("vercel", "claude-test", GatewayConfig("https://gateway.example", "key"), 1.0)
    lane = replace(options, max_token_cost=0.25)
    native_runtime._record_usage(
        lane, {"claude-test": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}}
    )
    assert total_tokens_from_history(options) == 10
    assert usage_by_model_from_history(options) == {"claude-test": (8, 2)}


@pytest.mark.parametrize("seed", [None, "seed"])
def test_native_without_aggregate_preserves_only_an_existing_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: str | None
) -> None:
    """An unscored empty output cannot become the result of a seedless run."""
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    session.no_incumbent = True
    ctx = _context(tmp_path, FakeRuntime(session))
    server = EvalServer(lambda *_: (1.0, {}), max_evals=2)
    if seed is None:
        with pytest.raises(ServiceError, match="fully evaluated candidate"):
            run_native_engine("autoresearch", Task(seed), server, ctx)
    else:
        result = run_native_engine("autoresearch", Task(seed), server, ctx)
        assert result.best_candidate == seed
        assert result.best_score is None
    assert session.closed


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_parent_rpc_checks_cumulative_budget_at_both_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """A known spending stop propagates even when RPC runs outside DSPy's context."""
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    ctx = _context(tmp_path, FakeRuntime(session))
    events: list[str] = []
    failure = CostCeilingExceededError("balance spent")

    def check() -> None:
        """Reject the configured evaluation boundary."""
        events.append("check")
        if boundary == "before" or "score" in events:
            raise failure

    def score(candidate: str, example: Any) -> tuple[float, dict[str, Any]]:
        """Record whether paid evaluation was admitted."""
        events.append("score")
        return 1.0, {}

    ctx.check_budget = check
    with pytest.raises(CostCeilingExceededError) as raised:
        run_native_engine("autoresearch", Task("seed"), EvalServer(score, max_evals=2), ctx)
    assert raised.value is failure
    assert events == (["check"] if boundary == "before" else ["check", "score", "check"])
    assert session.closed


def test_linux_cancellation_uses_procfs_when_ps_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill only the runner's descendants using kernel parent ids without procps."""
    for pid, parent in ((100, 1), (101, 100), (102, 101), (200, 1)):
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "status").write_text(f"Name:\ttest\nPPid:\t{parent}\n")
    monkeypatch.setattr(native_runner, "_PROC_ROOT", tmp_path)
    monkeypatch.setattr(native_runner.os, "getpid", lambda: 100)
    killed: list[int] = []
    monkeypatch.setattr(native_runner.os, "kill", lambda pid, signal: killed.append(pid))

    def no_ps(*args: Any, **kwargs: Any) -> None:
        """Reject accidental use of a binary absent from minimal images."""
        raise AssertionError("ps is unavailable")

    monkeypatch.setattr(native_runner.subprocess, "run", no_ps)
    native_runner._stop_children()
    assert killed == [102, 101]


def test_usage_prefers_cli_summaries_and_deduplicates_transcript_messages(tmp_path: Path) -> None:
    """Session mirrors and repeated assistant updates do not inflate token usage."""
    summary = {"session_id": "mh", "modelUsage": {"claude-test": {"inputTokens": 10, "outputTokens": 4}}}
    (tmp_path / "iter1_stdout.json").write_text(json.dumps(summary))
    mh_message = {
        "type": "assistant",
        "sessionId": "mh",
        "message": {"id": "m1", "model": "claude-test", "usage": {"input_tokens": 10, "output_tokens": 4}},
    }
    ar_message = {
        "type": "assistant",
        "sessionId": "ar",
        "message": {
            "id": "m2",
            "model": "claude-test",
            "usage": {"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 5},
        },
    }
    (tmp_path / "session.jsonl").write_text("\n".join(map(json.dumps, [mh_message, ar_message, ar_message])))
    usage = native_runner.collect_usage([tmp_path, tmp_path], "fallback")
    assert usage["claude-test"] == {
        "prompt_tokens": 13,
        "completion_tokens": 6,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 0,
        "total_tokens": 24,
        "calls": 2,
    }


def test_usage_counts_summary_turns_when_the_transcript_is_missing(tmp_path: Path) -> None:
    """A CLI summary with no mirrored transcript still contributes its request count."""
    summary = {
        "session_id": "solo",
        "num_turns": 7,
        "modelUsage": {"openai/gpt-test": {"inputTokens": 50, "outputTokens": 5}},
    }
    (tmp_path / "iter1_stdout.json").write_text(json.dumps(summary))
    usage = native_runner.collect_usage([tmp_path], "fallback")
    assert usage["openai/gpt-test"]["calls"] == 7
    assert usage["openai/gpt-test"]["prompt_tokens"] == 50


def test_artifact_restore_rejects_parent_traversal(tmp_path: Path) -> None:
    """An upstream-produced artifact cannot write outside the job artifact directory."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        member = tarfile.TarInfo("../escape.txt")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    session = FakeSession()
    session.files["native_artifacts.tar.gz.b64"] = base64.b64encode(data.getvalue()).decode()
    with pytest.raises(ServiceError, match="unsafe path"):
        native_runtime._restore_artifacts(session, tmp_path / "job")
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize(
    ("engine_id", "variant"),
    [
        ("autoresearch", "success"),
        ("meta_harness", "success"),
        ("meta_harness", "abort"),
        ("autoresearch", "repeated"),
    ],
)
def test_real_native_runner_drives_upstream_with_fake_cli(tmp_path: Path, engine_id: str, variant: str) -> None:
    """Exercise real upstream HTTP evaluation and session artifacts without any model or sandbox costs."""
    abort = variant == "abort"
    binary = tmp_path / "bin"
    binary.mkdir()
    fake_cli = binary / "claude"
    fake_cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, re, sys, urllib.request\n"
        "with (pathlib.Path.home()/'invocations.txt').open('a') as history: history.write('called\\n')\n"
        "args=sys.argv[1:]; session=args[args.index('--session-id')+1]\n"
        "if pathlib.Path('eval.sh').exists():\n"
        " script=pathlib.Path('eval.sh').read_text(); url=re.search(r'SERVER_URL=\"([^\"]+)\"',script).group(1)\n"
        " request=urllib.request.Request(url+'/evaluate',data=json.dumps({'candidate':'better'}).encode(),"
        "headers={'Content-Type':'application/json'})\n"
        " urllib.request.urlopen(request).read()\n"
        " if os.environ.get('FAKE_REPEAT'): urllib.request.urlopen(request).read()\n"
        "else:\n"
        " assert '--tools' in args and '--append-system-prompt' in args\n"
        " pathlib.Path('agents/better.txt').write_text('better')\n"
        " pathlib.Path('logs/run/pending_eval.json').write_text(json.dumps({'candidates':[{'name':'better','file':'agents/better.txt'}]}))\n"
        "out=pathlib.Path.home()/'.claude/projects/test'; out.mkdir(parents=True,exist_ok=True)\n"
        "message={'type':'assistant','sessionId':session,'message':{'id':'m1','model':'claude-test',"
        "'usage':{'input_tokens':7,'output_tokens':3}}}\n"
        "(out/(session+'.jsonl')).write_text(json.dumps(message)+'\\n')\n"
        "print(json.dumps({'total_cost_usd':0.01,'session_id':session,"
        "'modelUsage':{'claude-test':{'inputTokens':7,'outputTokens':3}}}))\n"
    )
    fake_cli.chmod(0o755)
    (tmp_path / "rpc").mkdir()
    payload = {
        "nonce": "testnonce",
        "engine_id": engine_id,
        "model": "claude-test",
        "sandbox": False,
        "max_token_cost": 0.05,
        "max_evals": 4,
        "max_concurrency": 1,
        "max_iterations": 3 if abort else 1,
        "timeout_seconds": 20,
        "task": {"name": "test", "seed_candidate": "seed"},
    }
    if engine_id == "meta_harness":
        payload["task"]["train_set"] = [{"id": "a"}, {"id": "b"}]
    else:
        # One research round keeps the fake's single scripted evaluation the whole run.
        payload["proposer"] = {"ralph": False}
    (tmp_path / "input.json").write_text(json.dumps(payload))
    env = {"PATH": f"{binary}{os.pathsep}/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONUNBUFFERED": "1"}
    if variant == "repeated":
        env["FAKE_REPEAT"] = "1"
    process = subprocess.Popen(
        [sys.executable, native_runner.__file__, "input.json"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    requests = 0
    try:
        for line in process.stdout:
            if not line.startswith("APP_NATIVE_RPC testnonce "):
                continue
            request = json.loads(line.split(" ", 2)[2])
            score = 0.8 if request["candidate"] == "better" else float(request["example"]["id"] == "a")
            if variant == "repeated" and requests == 0:
                score = 1.0
            failing = abort and request["candidate"] == "better"
            response = {"error": "parent scorer failed"} if failing else {"score": score, "info": {}}
            (tmp_path / "rpc" / f"{request['id']}.json").write_text(json.dumps(response))
            requests += 1
        stderr = process.stderr.read()
        assert process.wait(timeout=25) == int(abort), (tmp_path / "native_result.json").read_text() + stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    result = json.loads((tmp_path / "native_result.json").read_text())
    assert (tmp_path / "invocations.txt").read_text().splitlines() == ["called"]
    if abort:
        assert "parent scorer failed" in result["error"]
        assert result["interrupted_incumbent"]["best_candidate"] == "seed"
        assert result["interrupted_incumbent"]["best_score"] == 0.5
        assert result["usage_by_model"]["claude-test"]["total_tokens"] == 10
        return
    assert requests == (4 if engine_id == "meta_harness" else 2 if variant == "repeated" else 1)
    assert result["best_candidate"] == "better"
    assert result["best_score"] == (1.0 if variant == "repeated" else 0.8)
    assert result["usage_by_model"]["claude-test"]["total_tokens"] == 10
    assert result["usage_complete"] is True
    assert (tmp_path / "native_artifacts.tar.gz.b64").stat().st_size > 0


def test_autosaddler_bootstrap_pins_upstream_into_a_python_312_venv() -> None:
    """Install the pinned AutoSaddler revision on its own interpreter instead of the GEPA archive."""
    command = _bootstrap_command("vercel", engine_id="autosaddler")
    assert f"uv venv --python {native_runtime._AUTOSADDLER_PYTHON} native_venv" in command
    assert "uv pip install --python native_venv/bin/python --no-deps" in command
    assert native_runtime.AUTOSADDLER_REVISION in command
    assert f"sys.version_info >= {native_runtime.AUTOSADDLER_PYTHON_FLOOR!r}" in command
    assert "import autosaddler.v2.core.engine" in command
    assert "native_source.tar.gz.b64" not in command
    protected = _bootstrap_command("vercel", protected=True, engine_id="autosaddler")
    assert "uv venv" not in protected
    assert f"sys.version_info >= {native_runtime.AUTOSADDLER_PYTHON_FLOOR!r}" in protected
    assert "import autosaddler.v2.core.engine" in protected


def test_shinka_bootstrap_installs_the_pinned_requirements_into_a_python_312_venv() -> None:
    """Install the pinned ShinkaEvolve requirements on their own interpreter, without a proposer harness."""
    command = _bootstrap_command("vercel", engine_id="shinka_evolve")
    assert f"uv venv --python {native_runtime._AUTOSADDLER_PYTHON} native_venv" in command
    assert "--no-deps -r shinka_requirements.txt" in command
    assert "import shinka.core.async_runner" in command
    assert "native_source.tar.gz.b64" not in command
    assert "node -e" not in command
    protected = _bootstrap_command("vercel", protected=True, engine_id="shinka_evolve")
    assert "uv venv" not in protected
    assert "import shinka.core.async_runner" in protected
    assert f"if [ -x {native_runtime.SHINKA_IMAGE_PYTHON} ]" in protected


def test_dockerfile_bakes_shinka_into_the_venv_the_runtime_uses() -> None:
    """Keep the opt-in ShinkaEvolve venv at the runtime's path, built from the same pinned requirements."""
    dockerfile = (Path(__file__).resolve().parents[5] / "Dockerfile").read_text()
    venv = native_runtime.SHINKA_IMAGE_PYTHON.removesuffix("/bin/python")
    assert "ARG INCLUDE_SHINKA_EVOLVE=false" in dockerfile
    assert '[ "$INCLUDE_SHINKA_EVOLVE" = "1" ]' in dockerfile
    assert f"python -m venv {venv}" in dockerfile
    assert "core/service_gateway/optimization/blackbox/shinka_requirements.txt" in dockerfile
    assert f'{native_runtime.SHINKA_IMAGE_PYTHON} -c "import shinka.core.async_runner"' in dockerfile


def test_native_readiness_probes_shinka_in_the_image_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report ShinkaEvolve readiness from the image's own venv rather than the backend interpreter.

    Args:
        monkeypatch: Pytest fixture for replacing the pinned source archive.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "verified-source")
    session = ReadinessSession()
    options = NativeOptions(
        runtime="vercel",
        model="test/model",
        gateway=GatewayConfig(url="http://127.0.0.1:9000/v1", api_key="scoped"),
        budget_route={"url": "http://127.0.0.1:9000/v1", "token": "scoped"},
        sandbox_runtime=FakeRuntime(session),
        max_token_cost=1,
    )
    check_native_runtime(options)
    probe = session.calls[1][0]
    assert native_runtime.SHINKA_IMAGE_PYTHON in probe
    assert "os.X_OK" in probe
    assert "find_spec(" in probe


def test_shinka_runner_files_carry_the_pinned_requirements() -> None:
    """Ship the self-contained runner with a requirements file pinning the upstream release."""
    files = native_runtime._runner_files("shinka_evolve")
    assert set(files) == {
        "shinka_runner.py",
        "shinka_bundle.py",
        "harness_bridge.py",
        "repo_tree.py",
        "shinka_requirements.txt",
    }
    assert f"shinka-evolve=={native_runtime.SHINKA_VERSION}" in files["shinka_requirements.txt"]


def test_shinka_transport_routes_every_model_through_its_own_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hand each optimization route to the guest by variable, never in the payload or a network-edge header.

    Args:
        tmp_path: Artifact destination.
        monkeypatch: Pytest fixture for replacing the pinned source archive.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    session.candidate = {"system": "better"}
    runtime = FakeRuntime(session)
    ctx = _context(tmp_path, runtime)
    ctx.native_options = replace(
        ctx.native_options,
        shinka={"num_islands": 3},
        shinka_models=(
            {"model": "claude-test", "url": "https://gateway.example/v1", "token": "route-a"},
            {"model": "gpt-test", "url": "https://gateway.example/v1", "token": "route-b"},
        ),
    )
    result = run_native_engine(
        "shinka_evolve",
        Task({"system": "seed"}, cases=[{"id": "case"}]),
        EvalServer(lambda *_: (0.5, {}), max_evals=3),
        ctx,
    )

    payload = json.loads(session.files["native_input.json"])
    assert payload["shinka"] == {"num_islands": 3}
    assert payload["shinka_models"] == [
        {"model": "claude-test", "url": "https://gateway.example/v1", "key_env": "APP_SHINKA_KEY_0"},
        {"model": "gpt-test", "url": "https://gateway.example/v1", "key_env": "APP_SHINKA_KEY_1"},
    ]
    assert payload["shinka_meta_key_env"] == "APP_SHINKA_META_KEY"
    assert "route-a" not in session.files["native_input.json"]
    env = session.calls[1][1]["env"]
    assert (env["APP_SHINKA_KEY_0"], env["APP_SHINKA_KEY_1"]) == ("route-a", "route-b")
    assert env["APP_SHINKA_META_KEY"] == "route-a"
    assert env["SHINKA_PRICING_MODE"] == "offline"
    assert runtime.spec.inject_headers == {}
    assert session.calls[1][0].count("shinka_runner.py") == 1
    assert result.best_candidate == {"system": "better"}
    assert result.best_score == 0.5
    assert "shinka_editor" not in payload
    assert payload["proposer"]["max_tool_calls"] == 100


def test_shinka_agent_editor_ships_a_tagged_pi_launch_per_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prepare Pi and hand the guest one file-tools-only Pi launch per optimization route, billed as mutations.

    Args:
        tmp_path: Artifact destination.
        monkeypatch: Pytest fixture for replacing the pinned source archive.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    session.candidate = "better"
    ctx = _context(tmp_path, FakeRuntime(session))
    ctx.native_options = replace(
        ctx.native_options,
        proposer=BlackboxProposer(harness="pi", max_tool_calls=12),
        shinka={"editor": "agent"},
        shinka_models=(
            {"model": "claude-test", "url": "https://gateway.example/v1", "token": "route-a"},
            {"model": "gpt-test", "url": "https://gateway.example/v1", "token": "route-b"},
        ),
    )
    run_native_engine("shinka_evolve", Task("seed"), EvalServer(lambda *_: (0.5, {}), max_evals=3), ctx)

    payload = json.loads(session.files["native_input.json"])
    editor = payload["shinka_editor"]
    assert editor["max_tool_calls"] == 12
    assert [launch["key_env"] for launch in editor["launches"]] == ["APP_SHINKA_KEY_0", "APP_SHINKA_KEY_1"]
    launch = editor["launches"][0]
    assert launch["model"] == "claude-test"
    assert launch["output_format"] == "pi"
    assert "--tools read,grep,find,ls,edit,write" in launch["run_command"]
    assert "--no-extensions --no-skills --no-context-files" in launch["run_command"]
    models = json.loads(launch["files"][".app/pi/models.json"])
    provider = next(iter(models["providers"].values()))
    tags = json.loads(provider["headers"][USAGE_TAGS_HEADER])
    assert tags == {"caller": "proposer", "kind": "mutation"}
    assert "route-a" not in session.files["native_input.json"]
    assert "pi" in session.calls[0][0]
    assert "node -e" in session.calls[0][0]


def test_shinka_agent_editor_applies_the_effort_only_to_the_reflection_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reflection model's Pi editor runs at the chosen effort; the extra models keep the default.

    Args:
        tmp_path: Artifact destination.
        monkeypatch: Pytest fixture for replacing the pinned source archive.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    session.candidate = "better"
    ctx = _context(tmp_path, FakeRuntime(session))
    ctx.native_options = replace(
        ctx.native_options,
        proposer=BlackboxProposer(harness="pi"),
        shinka={"editor": "agent"},
        shinka_models=(
            {"model": "claude-test", "url": "https://gateway.example/v1", "token": "route-a"},
            {"model": "gpt-test", "url": "https://gateway.example/v1", "token": "route-b"},
        ),
        reasoning_effort="high",
    )
    run_native_engine("shinka_evolve", Task("seed"), EvalServer(lambda *_: (0.5, {}), max_evals=3), ctx)

    main, extra = json.loads(session.files["native_input.json"])["shinka_editor"]["launches"]
    assert '--model "$APP_MODEL:high"' in main["run_command"]
    assert '--model "$APP_MODEL"' in extra["run_command"]
    assert "thinkingLevelMap" not in json.loads(extra["files"][".app/pi/models.json"])["providers"]["app"]["models"][0]


def test_shinka_bootstrap_prepares_pi_only_for_the_agent_editor() -> None:
    """Install Pi and check Node for the agent editor, and skip both for single-call ShinkaEvolve."""
    plain = _bootstrap_command("vercel", engine_id="shinka_evolve", harness="pi", install_command="npm i -g pi@1")
    agent = _bootstrap_command(
        "vercel", engine_id="shinka_evolve", harness="pi", install_command="npm i -g pi@1", shinka_agent=True
    )
    assert "npm i -g pi@1" not in plain
    assert "npm i -g pi@1" in agent
    assert "node -e" in agent


def test_shinka_novelty_hands_the_judge_and_embeddings_their_own_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route duplicate rejection's judge and embeddings by variable, so their calls are tagged apart.

    Args:
        tmp_path: Artifact destination.
        monkeypatch: Pytest fixture for replacing the pinned source archive.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    session.candidate = "better"
    ctx = _context(tmp_path, FakeRuntime(session))
    ctx.native_options = replace(
        ctx.native_options,
        shinka={"novelty": True},
        shinka_models=({"model": "claude-test", "url": "https://gateway.example/v1", "token": "route-a"},),
        shinka_embedding={
            "model": "openai/text-embedding-3-small",
            "url": "https://gw.example/v1",
            "token": "route-emb",
        },
    )
    run_native_engine("shinka_evolve", Task("seed"), EvalServer(lambda *_: (0.5, {}), max_evals=3), ctx)

    payload = json.loads(session.files["native_input.json"])
    assert payload["shinka_embedding"] == {
        "model": "openai/text-embedding-3-small",
        "url": "https://gw.example/v1",
        "key_env": "APP_SHINKA_EMBED_KEY",
    }
    assert payload["shinka_novelty_key_env"] == "APP_SHINKA_NOVELTY_KEY"
    assert "route-emb" not in session.files["native_input.json"]
    env = session.calls[1][1]["env"]
    assert (env["APP_SHINKA_NOVELTY_KEY"], env["APP_SHINKA_EMBED_KEY"]) == ("route-a", "route-emb")


def test_shinka_ships_a_repository_checkout_with_its_bundle_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ship the repository tree and the bundle and tree helpers the runner loads beside itself.

    Args:
        tmp_path: Artifact destination.
        monkeypatch: Pytest fixture for replacing the pinned source archive.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    chunk = tmp_path / "tree.0000.b64"
    chunk.write_text("dHJlZQ==", encoding="ascii")
    session = FakeSession()
    session.candidate = "diff --git a/src/a.py b/src/a.py\n"
    ctx = _context(tmp_path, FakeRuntime(session))
    ctx.native_options = replace(
        ctx.native_options,
        repo={"chunks": [str(chunk)], "editable_paths": ["src"], "readonly_paths": ["vendor"]},
        shinka_models=({"model": "m", "url": "https://gateway.example/v1", "token": "t"},),
    )
    run_native_engine("shinka_evolve", Task(""), EvalServer(lambda *_: (0.5, {}), max_evals=3), ctx)

    payload = json.loads(session.files["native_input.json"])
    assert payload["repo"] == {
        "chunks": ["repo-tree/tree.0000.b64"],
        "editable_paths": ["src"],
        "readonly_paths": ["vendor"],
        "constrained": False,
    }
    assert {"shinka_runner.py", "shinka_bundle.py", "repo_tree.py"} <= set(session.files)


def test_autosaddler_runner_files_bundle_the_scenario_plugin() -> None:
    """Ship the self-contained runner with every prompt and skill of the Skynet plugin."""
    files = native_runtime._runner_files("autosaddler")
    assert set(files) == {
        "autosaddler_runner.py",
        "harness_bridge.py",
        "repo_tree.py",
        "autosaddler_plugin/LICENSE",
        "autosaddler_plugin/NOTICE.md",
        *(
            f"autosaddler_plugin/{prefix}{name}"
            for prefix in ("", "repo/")
            for name in (
                "SYSTEM.md",
                "prompts/diagnose_patch.md",
                "prompts/evolve.md",
                "prompts/reflect.md",
                "skills/candidate-patch/SKILL.md",
                "skills/patch-verification/SKILL.md",
            )
        ),
    }
    assert all(text.strip() for text in files.values())
    assert set(native_runtime._runner_files("meta_harness")) == {
        "native_runner.py",
        "native_engines.py",
        "harness_bridge.py",
        "repo_tree.py",
        "upstream_prompts/meta_harness/SKILL.md",
        "upstream_prompts/meta_harness/LICENSE",
        "upstream_prompts/meta_harness/NOTICE.md",
    }


def test_autosaddler_transport_scores_named_parts_without_the_gepa_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send the AutoSaddler runner and plugin, accept a parts candidate and record the upstream revision."""
    monkeypatch.setattr(native_runtime, "_source_archive", MagicMock(side_effect=AssertionError("unused")))
    session = FakeSession()
    session.candidate = {"system": "better", "user": "ask"}
    runtime = FakeRuntime(session)
    calls: list[tuple[Any, Any]] = []

    def score(candidate: Any, example: Any) -> tuple[float, dict[str, Any]]:
        """Record the parts candidate the parent scored."""
        calls.append((candidate, example))
        return 0.75, {}

    task = Task({"system": "seed", "user": "ask"}, cases=[{"id": "a"}, {"id": "b"}])
    result = run_native_engine("autosaddler", task, EvalServer(score, max_evals=3), _context(tmp_path, runtime))

    assert calls == [({"system": "better", "user": "ask"}, {"id": "case"})]
    assert result.best_candidate == {"system": "better", "user": "ask"}
    assert result.metadata["upstream_source"] == native_runtime.AUTOSADDLER_SOURCE
    assert result.metadata["upstream_revision"] == native_runtime.AUTOSADDLER_REVISION
    payload = json.loads(session.files["native_input.json"])
    assert payload["source"] == native_runtime.AUTOSADDLER_REVISION
    assert payload["task"]["seed_candidate"] == {"system": "seed", "user": "ask"}
    assert "native_source.tar.gz.b64" not in session.files
    assert "autosaddler_plugin/SYSTEM.md" in session.files
    assert "autosaddler_runner.py" in session.calls[1][0]
    assert "native_runner.py" not in session.calls[1][0]
    assert session.closed


def test_autosaddler_transport_rejects_a_candidate_of_the_wrong_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse to score text when the task carries named parts instead of crashing the scorer."""
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    runtime = FakeRuntime(session)
    scorer = MagicMock(return_value=(0.5, {}))
    task = Task({"system": "seed", "user": "ask"}, cases=[{"id": "a"}, {"id": "b"}])
    with pytest.raises(ServiceError, match="invalid candidate shape"):
        run_native_engine("autosaddler", task, EvalServer(scorer, max_evals=3), _context(tmp_path, runtime))
    scorer.assert_not_called()


@pytest.mark.parametrize(
    "task",
    [Task(None), Task("seed"), Task("seed", cases=[{"id": "a"}]), Task("seed", cases=[{"id": "a"}, {"id": "b"}])],
)
def test_autosaddler_transport_runs_with_zero_one_or_many_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: Task
) -> None:
    """AutoSaddler needs no cases and no seed: a blank run patches an empty text, cases go in once.

    Args:
        tmp_path: Workspace.
        monkeypatch: Skips packing the GEPA archive.
        task: The run's starting point and cases.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", MagicMock(side_effect=AssertionError("unused")))
    session = FakeSession()
    runtime = FakeRuntime(session)

    result = run_native_engine(
        "autosaddler", task, EvalServer(lambda *_: (0.5, {}), max_evals=3), _context(tmp_path, runtime)
    )

    assert result.best_candidate == "better"
    sent = json.loads(session.files["native_input.json"])["task"]
    assert sent["seed_candidate"] == (task.seed_candidate or "")
    assert sent["train_set"] == (task.cases or None)
    assert "val_set" not in sent


@pytest.mark.parametrize("engine_id", ["autoresearch", "meta_harness"])
def test_real_native_runner_drives_upstream_through_a_pi_proposer(tmp_path: Path, engine_id: str) -> None:
    """Run the unchanged upstream engines against a non-Claude harness through the ``claude`` shim."""
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "python3").symlink_to(sys.executable)
    fake_pi = binary / "pi"
    fake_pi.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, re, sys, urllib.request\n"
        "with (pathlib.Path.home()/'invocations.txt').open('a') as history: history.write(' '.join(sys.argv[1:7])+'\\n')\n"
        "assert pathlib.Path(os.environ['APP_PROMPT_FILE']).read_text()\n"
        "assert os.environ['APP_API_KEY'] == 'real-key' and os.environ['APP_MODEL'] == 'claude-test'\n"
        "assert pathlib.Path('AGENTS.md').read_text() and json.loads(pathlib.Path('.app/pi/models.json').read_text())\n"
        "if pathlib.Path('eval.sh').exists():\n"
        " script=pathlib.Path('eval.sh').read_text(); url=re.search(r'SERVER_URL=\"([^\"]+)\"',script).group(1)\n"
        " request=urllib.request.Request(url+'/evaluate',data=json.dumps({'candidate':'better'}).encode(),"
        "headers={'Content-Type':'application/json'})\n"
        " urllib.request.urlopen(request).read()\n"
        "else:\n"
        " pathlib.Path('agents/better.txt').write_text('better')\n"
        " pathlib.Path('logs/run/pending_eval.json').write_text(json.dumps({'candidates':[{'name':'better','file':'agents/better.txt'}]}))\n"
        "print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'done'}],"
        "'usage':{'input':7,'output':3}}}))\n"
    )
    fake_pi.chmod(0o755)
    (tmp_path / "rpc").mkdir()
    launch = build_launch(
        BlackboxTarget(kind="agent", harness="pi", model="claude-test"),
        GatewayConfig(url="https://gw.example/v1", api_key=harness_bridge.KEY_TOKEN),
    )
    payload = {
        "nonce": "testnonce",
        "engine_id": engine_id,
        "model": "claude-test",
        "sandbox": False,
        "max_token_cost": 0.05,
        "max_evals": 4,
        "max_concurrency": 1,
        "max_iterations": 1,
        "timeout_seconds": 20,
        "proposer": {
            **launch_payload(launch),
            "harness": "pi",
            "model": "claude-test",
            "price": {"input": 0.0, "output": 0.0},
            "ralph": False,
        },
        "task": {"name": "test", "seed_candidate": "seed"},
    }
    if engine_id == "meta_harness":
        payload["task"]["train_set"] = [{"id": "a"}, {"id": "b"}]
    (tmp_path / "input.json").write_text(json.dumps(payload))
    env = {
        "PATH": f"{binary}{os.pathsep}/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONUNBUFFERED": "1",
        harness_bridge.KEY_ENV: "real-key",
    }
    process = subprocess.Popen(
        [sys.executable, native_runner.__file__, "input.json"],
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
            score = 0.8 if request["candidate"] == "better" else float(request["example"]["id"] == "a")
            (tmp_path / "rpc" / f"{request['id']}.json").write_text(json.dumps({"score": score, "info": {}}))
        stderr = process.stderr.read()
        assert process.wait(timeout=25) == 0, (tmp_path / "native_result.json").read_text() + stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    result = json.loads((tmp_path / "native_result.json").read_text())
    invocations = (tmp_path / "invocations.txt").read_text().splitlines()
    assert len(invocations) == 1
    assert "--mode json" in invocations[0]
    assert result["best_candidate"] == "better"
    assert result["best_score"] == 0.8
    assert result["usage_by_model"]["claude-test"]["total_tokens"] == 10
    assert result["usage_complete"] is True
    assert (tmp_path / harness_bridge.SHIM_DIR / "claude").exists()
    assert harness_bridge.KEY_TOKEN not in next(tmp_path.rglob(".app/pi/models.json")).read_text()
    assert list((tmp_path / ".claude" / "projects").rglob("*.jsonl"))


def test_bootstrap_installs_only_the_selected_proposer_harness() -> None:
    """Install and probe only the chosen harness."""
    pi_bootstrap = _bootstrap_command("vercel", harness="pi", install_command="npm install -g pi@1.2.3")
    assert "npm install -g pi@1.2.3" in pi_bootstrap
    assert "pi --version" in pi_bootstrap
    protected = _bootstrap_command("vercel", protected=True, harness="pi", install_command="npm install -g pi@1.2.3")
    assert "npm install" not in protected
    assert "pi --version" in protected
    custom = _bootstrap_command("vercel", harness="custom", install_command="pip install my-agent")
    assert "pip install my-agent" in custom
    assert "--version" not in custom.split("pip install my-agent", 1)[1].split("native-python", 1)[0]
    assert "codex --version" in _bootstrap_command("vercel")


def test_run_native_engine_serializes_the_proposer_without_the_gateway_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ship the chosen harness launch and knobs in the payload while the key only travels through the env."""
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    runtime = FakeRuntime(session)
    ctx = _context(tmp_path, runtime)
    proposer = BlackboxProposer(harness="codex", max_candidates_per_iter=3, ralph=False)
    ctx.native_options = replace(ctx.native_options, proposer=proposer)
    run_native_engine("autoresearch", Task("seed"), EvalServer(lambda c, e: (0.5, {}), max_evals=3), ctx)
    payload = json.loads(session.files["native_input.json"])
    assert payload["proposer"]["harness"] == "codex"
    assert payload["proposer"]["output_format"] == "codex"
    assert payload["proposer"]["max_candidates_per_iter"] == 3
    assert payload["proposer"]["ralph"] is False
    assert list(payload["proposer"]["price"].values()) == list(model_token_costs("claude-test"))
    assert "secret" not in session.files["native_input.json"]
    assert harness_bridge.KEY_TOKEN in session.files["native_input.json"]
    assert session.calls[1][1]["env"][harness_bridge.KEY_ENV] == "app-managed"
    assert "codex --version" in session.calls[0][0]


def test_run_native_engine_hands_the_reasoning_effort_to_the_proposer_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The optimization model's effort reaches the config the proposer CLI reads in the sandbox."""
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()
    ctx = _context(tmp_path, FakeRuntime(session))
    ctx.native_options = replace(
        ctx.native_options, proposer=BlackboxProposer(harness="codex"), reasoning_effort="high"
    )
    run_native_engine("autoresearch", Task("seed"), EvalServer(lambda c, e: (0.5, {}), max_evals=3), ctx)
    payload = json.loads(session.files["native_input.json"])
    assert 'model_reasoning_effort = "high"' in payload["proposer"]["files"][".app/codex/config.toml"]


class _RecordingMailbox:
    """Child-side mailbox stand-in that scores by vowel density and records progress lines."""

    def __init__(self) -> None:
        """Start with no recorded progress."""
        self.lines: list[dict[str, Any]] = []
        self.labels = threading.local()

    def evaluate(self, candidate: Any, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Score a text by its vowel density, ignoring the case.

        Args:
            candidate: Text under evaluation.
            example: Ignored.

        Returns:
            Vowel density and empty side information.
        """
        return sum(ch in "aeiou" for ch in candidate) / max(1, len(candidate)), {}

    def emit(self, prefix: str, payload: dict[str, Any]) -> None:
        """Record one progress payload.

        Args:
            prefix: Ignored line prefix.
            payload: Progress payload the server reports.
        """
        self.lines.append(payload)


def test_child_server_streams_cases_of_a_full_sweep_then_the_version(tmp_path: Path) -> None:
    """A sweep over every visible case streams each case, and the version carries them all.

    Args:
        tmp_path: Upstream evaluation artifacts.
    """
    mailbox = _RecordingMailbox()
    task = UpstreamTask("seed", train_set=[{"id": "a"}, {"id": "b"}])
    server = native_runner.ProgressEvalServer(
        task, mailbox, OptimizeAnythingConfig(max_evals=8, max_concurrency=1), tmp_path
    )

    server.evaluate_examples("aeiou", split="train")
    server.log_progress(1.0, candidate="aeiou")
    server.evaluate_examples("xyz", example_ids=["a"])
    server.log_progress(0.0, candidate="xyz")

    cases = [line for line in mailbox.lines if line.get("event") == "case_scored"]
    versions = [line for line in mailbox.lines if "candidate" in line]
    assert [(line["candidate_id"], line["example_id"], line["score"], line["total"]) for line in cases] == [
        (0, "0", 1.0, 2),
        (0, "1", 1.0, 2),
    ]
    assert [line["candidate_id"] for line in versions] == [0, 1]
    assert versions[0]["per_example"] == [("0", 1.0), ("1", 1.0)]
    assert versions[0]["total_evals"] == 2
    assert versions[1]["per_example"] == []


def test_mailbox_relays_case_scores_and_per_case_versions() -> None:
    """Parent-side progress lines become case_scored and candidate events with case scores."""
    sink: list[tuple[str, dict[str, Any]]] = []
    mailbox = native_runtime._EvaluatorMailbox(
        FakeSession(),
        EvalServer(lambda *_: (1.0, {}), max_evals=3),
        "nonce",
        progress_callback=lambda event, metrics: sink.append((event, metrics)),
    )

    mailbox.on_output(
        "stdout",
        'APP_NATIVE_PROGRESS nonce {"event": "case_scored", "candidate_id": 0, "example_id": "1", "score": 0.5, "total": 2}\n'
        'APP_NATIVE_PROGRESS nonce {"event": "case_scored", "candidate_id": true, "example_id": "0", "score": 1.0, "total": 2}\n'
        'APP_NATIVE_PROGRESS nonce {"candidate_id": 0, "candidate": "seed", "score": 0.75, "total_evals": 2, '
        '"per_example": [["0", 1.0], ["1", 0.5], ["2", null]]}\n',
    )

    assert mailbox.error is None
    assert sink[0] == (PROGRESS_CASE_SCORED, {"trial": 0, "example_id": "1", "score": 0.5, "total": 2})
    assert len(sink) == 2
    event, candidate = sink[1]
    assert event == PROGRESS_CANDIDATE
    assert candidate["candidate_id"] == "0"
    assert candidate["parent_id"] is None
    assert candidate["score"] == 0.75
    assert candidate["per_example"] == [{"id": "0", "score": 1.0}, {"id": "1", "score": 0.5}]


def test_mailbox_shows_no_score_for_a_version_with_a_crashed_case() -> None:
    """The child's filled mean for a version with a crashed case never reaches the candidate tree (SKY-367)."""

    def crash_on_case_two(candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Raise on case 2, else score 1.0.

        Args:
            candidate: Candidate text.
            case: The case.

        Returns:
            A full score and no side information.

        Raises:
            RuntimeError: On case 2.
        """
        if case == {"id": "2"}:
            raise RuntimeError("scorer bug")
        return 1.0, {}

    sink: list[tuple[str, dict[str, Any]]] = []
    server = EvalServer(crash_on_case_two, max_evals=3)
    server.evaluate("seed", {"id": "2"})
    mailbox = native_runtime._EvaluatorMailbox(
        FakeSession(), server, "nonce", progress_callback=lambda event, metrics: sink.append((event, metrics))
    )

    mailbox.on_output(
        "stdout",
        'APP_NATIVE_PROGRESS nonce {"candidate_id": 0, "candidate": "seed", "score": 0.75, "total_evals": 2, '
        '"per_example": [["0", 1.0], ["1", 0.5], ["2", 0.75]]}\n',
    )

    assert mailbox.error is None
    [(event, candidate)] = sink
    assert event == PROGRESS_CANDIDATE
    assert (candidate["score"], candidate["per_example"]) == (None, [])


def test_mailbox_relays_the_lineage_an_engine_reports() -> None:
    """A version's parent and generation reach the candidate event; engines that omit them keep the defaults."""
    sink: list[tuple[str, dict[str, Any]]] = []
    mailbox = native_runtime._EvaluatorMailbox(
        FakeSession(),
        EvalServer(lambda *_: (1.0, {}), max_evals=3),
        "nonce",
        progress_callback=lambda event, metrics: sink.append((event, metrics)),
    )

    mailbox.on_output(
        "stdout",
        'APP_NATIVE_PROGRESS nonce {"candidate_id": 4, "candidate": "x", "score": 0.5, "total_evals": 1, '
        '"parent_id": 1, "generation": 2}\n'
        'APP_NATIVE_PROGRESS nonce {"candidate_id": 5, "candidate": "y", "score": 0.5, "total_evals": 2, '
        '"parent_id": null, "generation": true}\n',
    )

    assert mailbox.error is None
    candidates = [metrics for event, metrics in sink if event == PROGRESS_CANDIDATE]
    assert [(c["candidate_id"], c["parent_id"], c["generation"]) for c in candidates] == [
        ("4", "1", 2),
        ("5", None, 0),
    ]


def test_a_versions_agent_tool_calls_reach_the_run_log(caplog: pytest.LogCaptureFixture) -> None:
    """Log how many tool calls the agent spent on a version, and only when the child reports them.

    Args:
        caplog: Captures the run log.
    """
    mailbox = native_runtime._EvaluatorMailbox(FakeSession(), EvalServer(lambda *_: (1.0, {}), max_evals=3), "nonce")

    with caplog.at_level(logging.INFO, logger=native_runtime.logger.name):
        mailbox.on_output(
            "stdout",
            'APP_NATIVE_PROGRESS nonce {"candidate_id": 1, "candidate": "x", "score": 0.5, "total_evals": 1, '
            '"tool_calls": 12, "tool_cap_reached": false}\n'
            'APP_NATIVE_PROGRESS nonce {"candidate_id": 2, "candidate": "y", "score": 0.6, "total_evals": 2, '
            '"tool_calls": 100, "tool_cap_reached": true}\n'
            'APP_NATIVE_PROGRESS nonce {"candidate_id": 3, "candidate": "z", "score": 0.7, "total_evals": 3}\n',
        )

    lines = [record.getMessage() for record in caplog.records if "scored" in record.getMessage()]
    assert lines == [
        "Candidate 1 scored 0.5000 after 1 evaluations; the agent used 12 tool call(s)",
        "Candidate 2 scored 0.6000 after 2 evaluations; the agent used 100 tool call(s), hitting the cap",
        "Candidate 3 scored 0.7000 after 3 evaluations",
    ]


def test_each_evaluation_runs_in_a_scope_of_the_candidate_and_case_the_child_names() -> None:
    """Scorer events logged while scoring a request carry its candidate and case labels."""
    seen: list[dict[str, Any] | None] = []

    def evaluator(candidate: str, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Record the event scope the scorer would run in."""
        seen.append(sandbox_log._scope.get())
        return 1.0, {}

    mailbox = native_runtime._EvaluatorMailbox(FakeSession(), EvalServer(evaluator, max_evals=3), "nonce")
    request = {"candidate": "seed", "example": {"q": 1}, "candidate_id": 4, "case": "2"}
    mailbox.on_output(
        "stdout",
        f"APP_NATIVE_RPC nonce {json.dumps({**request, 'id': '0' * 32})}\n"
        f"APP_NATIVE_RPC nonce {json.dumps({**request, 'id': '1' * 32, 'candidate_id': True, 'case': [1]})}\n",
    )
    assert mailbox.error is None
    assert seen == [{"candidate": "4", "case": "2"}, {}]


def test_the_child_names_the_candidate_and_case_of_each_request(tmp_path: Path) -> None:
    """Each evaluation is labelled with the registered candidate id and the case's position."""
    labels: list[tuple[Any, Any]] = []
    mailbox = _RecordingMailbox()

    def evaluate(candidate: str, example: Any = None, **kwargs: Any) -> tuple[float, dict[str, Any]]:
        """Record the labels the request would carry."""
        labels.append((mailbox.labels.candidate_id, mailbox.labels.case))
        return 1.0, {}

    mailbox.evaluate = evaluate  # type: ignore[method-assign]
    task = UpstreamTask("seed", train_set=[{"id": "a"}, {"id": "b"}])
    server = native_runner.ProgressEvalServer(
        task, mailbox, OptimizeAnythingConfig(max_evals=8, max_concurrency=1), tmp_path
    )
    server._register_candidate("seed")
    server.evaluate("seed", list(server._examples.values())[1])
    server.evaluate("unregistered", None)
    assert labels == [(0, "1"), (None, None)]


def test_a_sweep_fills_a_null_case_with_the_scored_mean(tmp_path: Path) -> None:
    """A case that always crashes leaves a sweep at the scored cases' mean, so versions still rank (SKY-367).

    Args:
        tmp_path: Upstream evaluation artifacts.
    """
    mailbox = _RecordingMailbox()

    def evaluate(candidate: str, example: Any = None, **kwargs: Any) -> tuple[float, dict[str, Any]]:
        """Score by vowel density, and give the parent's null on case ``c``."""
        if example == {"id": "c"}:
            return float("-inf"), {}
        return sum(ch in "aeiou" for ch in candidate) / len(candidate), {}

    mailbox.evaluate = evaluate  # type: ignore[method-assign]
    task = UpstreamTask("seed", train_set=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
    server = native_runner.ProgressEvalServer(
        task, mailbox, OptimizeAnythingConfig(max_evals=20, max_concurrency=1), tmp_path
    )

    good, good_info = server.evaluate_examples("aeiou", split="train")
    poor, poor_info = server.evaluate_examples("axyz", split="train")
    only_null, only_null_info = server.evaluate_examples("aeiou", example_ids=["c"])

    assert good == 1.0
    assert good_info["scores"] == {"a": 1.0, "b": 1.0, "c": 1.0}
    assert poor == 0.25
    assert poor_info["scores"]["c"] == 0.25
    assert good_info[native_engines.UNSCORED_CASES_KEY] == ["c"]
    assert good > poor
    assert only_null == float("-inf")
    assert only_null_info["scores"] == {"c": float("-inf")}


def _null_on(nulls: dict[str, set[str]]) -> _RecordingMailbox:
    """Build a mailbox that gives the parent's null on chosen cases of chosen versions.

    Args:
        nulls: Case ids that get a null score, keyed by version text.

    Returns:
        A recording mailbox that scores every other case by vowel density.
    """
    mailbox = _RecordingMailbox()

    def evaluate(candidate: str, example: Any = None, **kwargs: Any) -> tuple[float, dict[str, Any]]:
        """Give negative infinity, as the mailbox does for a parent null, on the chosen cases."""
        if example is not None and example["id"] in nulls.get(candidate, set()):
            return float("-inf"), {}
        return sum(ch in "aeiou" for ch in candidate) / max(1, len(candidate)), {}

    mailbox.evaluate = evaluate  # type: ignore[method-assign]
    return mailbox


def _child_server(mailbox: _RecordingMailbox, tmp_path: Path, max_evals: int = 20) -> Any:
    """Build the child-side evaluation server over cases ``a`` and ``b``.

    Args:
        mailbox: Mailbox the server scores through.
        tmp_path: Upstream evaluation artifacts.
        max_evals: Evaluation budget.

    Returns:
        The child's progress evaluation server.
    """
    task = UpstreamTask("seed", train_set=[{"id": "a"}, {"id": "b"}])
    config = OptimizeAnythingConfig(max_evals=max_evals, max_concurrency=1)
    return native_runner.ProgressEvalServer(task, mailbox, config, tmp_path)


def test_native_fill_unscored_fills_per_version() -> None:
    """A null case takes its own version's scored mean; a version with no scored case stays negative infinity."""
    inf = float("-inf")

    assert native_engines.fill_unscored(["x", "x", "y", "y"], [0.2, inf, inf, inf]) == [0.2, 0.2, inf, inf]


def test_best_logged_version_skips_a_version_with_no_scored_case(tmp_path: Path) -> None:
    """The logged-aggregate fallback never picks a crashed version and ranks the rest by scored cases (SKY-367).

    Args:
        tmp_path: Upstream evaluation artifacts.
    """
    server = _child_server(_null_on({"zzzz": {"a", "b"}, "aeio": {"b"}}), tmp_path)
    for candidate in ("zzzz", "aeio", "axyz"):
        score, _ = server.evaluate_examples(candidate, split="train")
        server.log_progress(score, candidate=candidate)

    assert native_engines.best_aggregate_candidate(server) == ("aeio", 1.0)


def test_best_valset_version_skips_a_version_with_no_scored_case() -> None:
    """GEPA's budget-stop fallback keeps only finite full-valset averages (SKY-367)."""
    tracker = native_engines.BestValsetVersion()

    for text, score in (("crashed", float("-inf")), ("ok", 0.25), ("partial", 0.9)):
        tracker.on_valset_evaluated(
            {
                "candidate": {"candidate": text},
                "average_score": score,
                "num_examples_evaluated": 1 if text == "partial" else 2,
                "total_valset_size": 2,
            }
        )

    assert tracker.best == ("ok", 0.25)


def test_best_of_n_repo_marks_a_partly_crashed_sample_and_ranks_the_rest(tmp_path: Path) -> None:
    """A sample with any null case is a metric_error sample, so its filled mean cannot stop the run (SKY-367).

    Args:
        tmp_path: Upstream evaluation artifacts.
    """
    server = _child_server(_null_on({"zzzz": {"a", "b"}, "aeio": {"b"}}), tmp_path, max_evals=6)
    patches = iter(["zzzz", "aeio", "axyz"])
    engine = native_engines.BestOfNRepoEngine.__new__(native_engines.BestOfNRepoEngine)
    engine.proposer = lambda *_: {"candidate": next(patches)}  # type: ignore[assignment]
    engine.proposer.total_cost = 0.0  # type: ignore[attr-defined]
    engine.proposer.session_ids = []  # type: ignore[attr-defined]
    engine.max_token_cost = None
    engine.stop_at_score = 0.9
    engine.samples = []
    engine.best = None

    result = engine.run(server.task, server)

    assert [sample.get("outcome") for sample in result.metadata["samples"]] == ["metric_error", "metric_error", None]
    assert [sample.get("score") for sample in result.metadata["samples"]] == [None, None, 0.25]
    assert (result.best_candidate, result.best_score) == ("axyz", 0.25)


def test_meta_harness_stop_ignores_a_filled_mean(tmp_path: Path) -> None:
    """A Meta-Harness version with a crashed case cannot reach ``stop_at_score`` (SKY-367).

    Args:
        tmp_path: Upstream evaluation artifacts and the engine's logs.
    """
    server = _child_server(_null_on({"aeio": {"b"}}), tmp_path)
    engine = native_engines.MetaHarnessEngine.__new__(native_engines.MetaHarnessEngine)
    engine.example_ids = native_engines.visible_example_ids(server)
    engine.logs_dir = tmp_path / "logs"
    engine.results = {}

    engine._benchmark(server, "crashed", "aeio")
    engine._benchmark(server, "ok", "axyz")

    assert engine._best() == ("aeio", 1.0)
    assert engine._best(scored=True) == ("axyz", 0.25)
    assert not native_engines._reached(0.9, engine._best(scored=True)[1])
    assert engine.incumbent(server) == ("axyz", 0.25)

    engine._write_frontier()
    frontier = json.loads((engine.logs_dir / "frontier_val.json").read_text(encoding="utf-8"))
    assert frontier["_pareto"] == [{"system": "ok", "val_accuracy": 0.25}]
    assert {frontier[eid]["system"] for eid in engine.example_ids} == {"ok"}


def test_meta_harness_falls_back_to_the_seed_when_every_version_crashed(tmp_path: Path) -> None:
    """With no fully scored version, Meta-Harness returns the seed, not the best filled average (SKY-367).

    Args:
        tmp_path: Upstream evaluation artifacts and the engine's logs.
    """
    server = _child_server(_null_on({"aeio": {"b"}, "eexx": {"b"}}), tmp_path)
    engine = native_engines.MetaHarnessEngine.__new__(native_engines.MetaHarnessEngine)
    engine.example_ids = native_engines.visible_example_ids(server)
    engine.logs_dir = tmp_path / "logs"
    engine.results = {}

    engine._benchmark(server, "seed", "eexx")
    engine._benchmark(server, "crashed", "aeio")

    assert engine.incumbent(server) == ("eexx", 0.5)


def test_meta_harness_prefers_a_partly_crashed_seed_over_a_fully_crashed_version(tmp_path: Path) -> None:
    """A version with no scored case counts as crashed, so the seed leads and the frontier leaves it out (SKY-367).

    Args:
        tmp_path: Upstream evaluation artifacts and the engine's logs.
    """
    server = _child_server(_null_on({"eexx": {"b"}, "zzzz": {"a", "b"}}), tmp_path)
    engine = native_engines.MetaHarnessEngine.__new__(native_engines.MetaHarnessEngine)
    engine.example_ids = native_engines.visible_example_ids(server)
    engine.logs_dir = tmp_path / "logs"
    engine.results = {}
    engine.session_ids = []
    engine.cost_usd = 0.0
    engine.work_dir = tmp_path / "work"

    engine._benchmark(server, "seed", "eexx")
    engine._benchmark(server, "broken", "zzzz")

    assert engine.results["broken"]["crashed"]
    assert engine._best(scored=True) is None
    assert engine.incumbent(server) == ("eexx", 0.5)
    result = engine._result(server, 1, "budget")
    assert (result.best_candidate, result.best_score) == ("eexx", 0.5)

    engine._write_frontier()
    frontier = json.loads((engine.logs_dir / "frontier_val.json").read_text(encoding="utf-8"))
    assert "broken" not in {row["system"] for row in frontier["_pareto"]}
    assert "broken" not in {frontier[eid]["system"] for eid in engine.example_ids if eid in frontier}


def _autoresearch(server: Any, tmp_path: Path, seed: str) -> Any:
    """Build an AutoResearch engine that records the server's sweeps.

    Args:
        server: Child evaluation server.
        tmp_path: Folder for the engine's lab.
        seed: The run's seed text.

    Returns:
        The engine, already recording.
    """
    engine = native_engines.AutoResearchEngine.__new__(native_engines.AutoResearchEngine)
    engine.repo = None
    engine.suffix = ".txt"
    engine.work_dir = tmp_path / "lab"
    (engine.work_dir / "archive").mkdir(parents=True)
    engine.round = 0
    engine.observations = []
    engine._ids = {}
    engine._lock = threading.Lock()
    engine.example_ids = native_engines.visible_example_ids(server)
    engine.seed = seed
    engine._record(server)
    return engine


def test_autoresearch_does_not_count_a_fully_crashed_sweep_as_full(tmp_path: Path) -> None:
    """A sweep with no scored case does not rank, so the incumbent falls back to the seed (SKY-367).

    Args:
        tmp_path: Upstream evaluation artifacts and the engine's lab folder.
    """
    server = _child_server(_null_on({"eexx": {"b"}, "zzzz": {"a", "b"}}), tmp_path)
    engine = _autoresearch(server, tmp_path, "eexx")

    for candidate in ("eexx", "zzzz"):
        score, _ = server.evaluate_examples(candidate)
        server.log_progress(score, candidate=candidate)

    assert engine._ranking() == {}
    assert engine.incumbent(server) == ("eexx", 0.5)


def test_autoresearch_incumbent_ignores_a_crashed_resweep_of_a_ranked_version(tmp_path: Path) -> None:
    """A ranked version's crashed re-sweep logs a higher filled mean, but the incumbent keeps the full score (SKY-367).

    Args:
        tmp_path: Upstream evaluation artifacts and the engine's lab folder.
    """
    mailbox = _RecordingMailbox()
    sweeps: dict[str, int] = {}

    def evaluate(candidate: str, example: Any = None, **kwargs: Any) -> tuple[float, dict[str, Any]]:
        """Score case ``a`` 0.2 and ``b`` 0.8, with a null on ``a`` in the version's second sweep."""
        if example["id"] == "a":
            sweeps[candidate] = sweeps.get(candidate, 0) + 1
            return (float("-inf"), {}) if sweeps[candidate] == 2 else (0.2, {})
        return 0.8, {}

    mailbox.evaluate = evaluate  # type: ignore[method-assign]
    server = _child_server(mailbox, tmp_path)
    engine = _autoresearch(server, tmp_path, "seed")

    for _ in range(2):
        score, _ = server.evaluate_examples("ab")
        server.log_progress(score, candidate="ab")

    assert native_engines.best_aggregate_candidate(server, only={"ab"}) == ("ab", 0.8)
    assert engine.incumbent(server) == ("ab", 0.5)


def test_autoresearch_leader_ignores_a_filled_mean(tmp_path: Path) -> None:
    """An AutoResearch version with a crashed case does not rank or stop the run (SKY-367).

    Args:
        tmp_path: Upstream evaluation artifacts and the engine's lab folder.
    """
    server = _child_server(_null_on({"aeio": {"b"}}), tmp_path)
    engine = native_engines.AutoResearchEngine.__new__(native_engines.AutoResearchEngine)
    engine.repo = None
    engine.suffix = ".txt"
    engine.work_dir = tmp_path / "lab"
    (engine.work_dir / "archive").mkdir(parents=True)
    engine.round = 0
    engine.observations = []
    engine._ids = {}
    engine._lock = threading.Lock()
    engine.example_ids = native_engines.visible_example_ids(server)
    engine._record(server)

    for candidate in ("aeio", "axyz"):
        score, _ = server.evaluate_examples(candidate)
        server.log_progress(score, candidate=candidate)

    assert engine._ranking() == {"axyz": 0.25}
    assert not native_engines._reached(0.9, engine._leader_score())
    engine.seed = "eexx"
    assert engine.incumbent(server) == ("axyz", 0.25)


def test_autoresearch_falls_back_to_the_seed_when_every_version_crashed(tmp_path: Path) -> None:
    """With no fully scored version, AutoResearch returns the seed, not the best filled mean (SKY-367).

    Args:
        tmp_path: Upstream evaluation artifacts and the engine's lab folder.
    """
    server = _child_server(_null_on({"aeio": {"b"}, "eexx": {"b"}}), tmp_path)
    engine = native_engines.AutoResearchEngine.__new__(native_engines.AutoResearchEngine)
    engine.repo = None
    engine.suffix = ".txt"
    engine.work_dir = tmp_path / "lab"
    (engine.work_dir / "archive").mkdir(parents=True)
    engine.round = 0
    engine.observations = []
    engine._ids = {}
    engine._lock = threading.Lock()
    engine.example_ids = native_engines.visible_example_ids(server)
    engine.seed = "eexx"
    engine._record(server)

    for candidate in ("eexx", "aeio"):
        score, _ = server.evaluate_examples(candidate)
        server.log_progress(score, candidate=candidate)

    assert engine.incumbent(server) == ("eexx", 0.5)


def test_scored_threshold_stopper_ignores_a_crashed_version() -> None:
    """Only a version with no crashed case can reach ``stop_at_score`` (SKY-367)."""
    state = SimpleNamespace(
        program_candidates=[{"current_candidate": "crashed"}, {"current_candidate": "ok"}],
        program_full_scores_val_set=[1.0, 0.5],
    )
    crashed = lambda candidate: native_engines.unwrap_text_candidate(candidate) == "crashed"  # noqa: E731

    assert not native_engines.ScoredThresholdStopper(0.9, crashed)(state)
    assert native_engines.ScoredThresholdStopper(0.5, crashed)(state)


def test_unsupported_runtime_reason_names_the_cloud_sandbox_not_its_vendor() -> None:
    """Keep the sandbox vendor out of the reason a user sees for an unsupported runtime."""
    reason = native_runtime.native_runtime_unavailable_reason("docker", SimpleNamespace(local_mode=False))

    assert reason == "Production optimizations run in the managed cloud sandbox."
    assert "vercel" not in reason.lower()

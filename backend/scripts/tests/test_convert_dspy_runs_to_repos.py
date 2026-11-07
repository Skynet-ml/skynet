"""Tests for the DSPy-run-to-repository conversion script (SKY-316).

All data is synthetic. No test opens a database, GitHub or the network:
loaders, token lookup and repository creation are stubs.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from core.connectors.github_create_repo import RepoCreateError
from core.models.blackbox import BlackboxRunRequest
from core.service_gateway.optimization.blackbox.repo_constraints import parse_constraints
from scripts.convert_dspy_runs_to_repos import (
    PLACEHOLDER_REPOSITORY,
    ConversionSkippedError,
    convert_job,
    main,
    map_strategy,
    plan_move_sql,
    plan_moves,
)

TEXT_RUN_CODE = """
def run(artifact, input):
    return {"answer": f"{artifact}|{input['question']}"}
"""

DSPY_RUN_CODE = """
import dspy


class QA(dspy.Module):
    def __init__(self):
        super().__init__()
        self.answer = dspy.Predict("question -> answer")


program = QA()


def run(artifact, input):
    return dspy.Prediction(answer=program.answer.signature.instructions + "|" + input["question"])
"""

METRIC_CODE = """
def score(input, output, expected=None):
    answer = output["answer"]
    hit = 1.0 if answer.endswith(str(expected)) else 0.0
    return hit, {"feedback": "match" if hit else "no match", "scores": {"exact": hit}}
"""

MODEL = {"name": "openai/gpt-4o-mini", "token_source": "managed"}


def make_job(**overrides: Any) -> dict[str, Any]:
    """Build a synthetic stored unified job.

    Args:
        **overrides: Top-level payload keys to replace.

    Returns:
        A ``jobs`` row as the script reads it.
    """
    payload: dict[str, Any] = {
        "payload_kind": "unified_v1",
        "name": "Support QA",
        "description": "Answer support questions.",
        "username": "alice",
        "task": {
            "objective": "Answer the question.",
            "background": "Synthetic test data.",
            "artifact": {"kind": "dspy_program", "starting_version": {"answer": "Start."}},
        },
        "data": {
            "mode": "inline",
            "cases": [
                {"input": {"question": "q1"}, "expected": "q1"},
                {"input": {"question": "q2"}, "expected": "zzz"},
            ],
        },
        "optimizer": {"choice": "dspy_gepa", "budget": {"max_scorer_runs": 50}},
        "models": {"task_model": MODEL, "reflection_model": {"name": "openai/gpt-4o"}},
        "workspace": {"run_code": DSPY_RUN_CODE, "metric_code": METRIC_CODE, "metric_timeout_seconds": 30},
        "seed": 7,
    }
    payload.update(overrides)
    return {
        "optimization_id": "job-0001-aaaa",
        "username": "alice",
        "optimization_type": "blackbox",
        "status": "success",
        "payload": payload,
        "result": {"best_candidate": {"answer": "Answer briefly.", "__dspy_state__": '{"answer": {"demos": [1]}}'}},
    }


def write_repo(root: Path, files: dict[str, str]) -> Path:
    """Write conversion files to a folder.

    Args:
        root: Target folder.
        files: Repository path to text.

    Returns:
        The folder.
    """
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def run_python(repo: Path, code: str) -> Any:
    """Run code in an isolated Python process with the repo on its path.

    Args:
        repo: The starter repository.
        code: Code that prints one JSON value.

    Returns:
        The decoded JSON value.
    """
    prelude = f"import json, sys; sys.path.insert(0, {str(repo)!r}); "
    done = subprocess.run(
        [sys.executable, "-I", "-c", prelude + code],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_sample_run_converts_to_repo_files_and_valid_request() -> None:
    """Convert a DSPy run: expected files, no demos, and a valid repository request."""
    conversion = convert_job(make_job())
    files = conversion["files"]
    assert set(files) == {
        "optimize.toml",
        "README.md",
        "requirements.txt",
        "program/__init__.py",
        "program/pipeline.py",
        "program/prompts.json",
        "metric.py",
        "scorer.py",
        "serve.py",
        "data/cases.jsonl",
    }
    assert json.loads(files["program/prompts.json"]) == {"answer": "Answer briefly."}
    assert "__dspy_state__" not in json.dumps(conversion["request"])
    assert "__dspy_state__" not in "".join(files.values())
    assert any("demos" in warning for warning in conversion["warnings"])
    tomllib.loads(files["optimize.toml"])
    constraints = parse_constraints(files["optimize.toml"].encode())
    assert constraints.editable_paths == ("program",)

    request = BlackboxRunRequest.model_validate(conversion["request"])
    assert request.recipe == "repo"
    assert request.target.kind == "repo"
    assert request.target.repo is not None
    assert request.target.repo.repository == PLACEHOLDER_REPOSITORY
    assert request.target.repo.editable_paths == ["program"]
    assert request.scorer.model is not None
    assert request.scorer.model.name == MODEL["name"]
    assert request.reflection_model_settings.name == "openai/gpt-4o"
    assert request.task_model_settings is None
    assert request.strategy.mode == "auto"
    assert request.seed_candidate == ""
    assert len(request.cases or []) == 2


def test_extra_files_go_under_program() -> None:
    """Place workspace helper files inside the editable program folder."""
    job = make_job()
    job["payload"]["workspace"]["files"] = {"helpers/util.py": "X = 1\n"}
    assert convert_job(job)["files"]["program/helpers/util.py"] == "X = 1\n"


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("dspy_gepa", {"mode": "auto"}),
        ("dspy_miprov2", {"mode": "auto"}),
        ("dspy_bootstrap_fewshot", {"mode": "auto"}),
        ("auto", {"mode": "auto"}),
        ("gepa", {"mode": "single", "engine": "gepa"}),
        ("shinka_evolve", {"mode": "single", "engine": "shinka_evolve"}),
    ],
)
def test_optimizer_choice_maps_to_repo_strategy(choice: str, expected: dict[str, Any]) -> None:
    """Map DSPy optimizers to Auto and keep repository engines."""
    strategy, warning = map_strategy(choice)
    assert strategy == expected
    assert (warning is not None) == choice.startswith("dspy_")


def test_iteration_limit_dropped_for_auto() -> None:
    """Drop an iteration cap that the mapped strategy cannot honor."""
    job = make_job(optimizer={"choice": "dspy_miprov2", "budget": {"max_scorer_runs": 10, "max_iterations": 3}})
    conversion = convert_job(job)
    assert "max_iterations" not in conversion["request"]["budget"]
    BlackboxRunRequest.model_validate(conversion["request"])


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda job: job["payload"]["data"].update(cases=None, staged_dataset_id="ds-1"), "staged dataset"),
        (lambda job: job.update(optimization_type="run", payload={"optimization_type": "run"}), "legacy"),
        (lambda job: job["payload"]["workspace"].update(run_code=""), "no run code"),
        (
            lambda job: job["payload"]["workspace"].update(
                metric_code="from skynet import observation\ndef score(i, o):\n    return 1.0, {'feedback': 'x'}\n"
            ),
            "observation",
        ),
        (lambda job: job["payload"]["workspace"].update(files={"pipeline.py": ""}), "clash"),
    ],
)
def test_unconvertible_jobs_are_skipped(mutate: Any, reason: str) -> None:
    """Skip a staged dataset, a legacy run, missing run code, observation helpers and file clashes."""
    job = make_job()
    mutate(job)
    with pytest.raises(ConversionSkippedError, match=reason):
        convert_job(job)


def test_plan_moves_follow_the_incumbent_after_a_cycle() -> None:
    """Move a plan that ran a cycle to its converted incumbent and restart its baseline."""
    converted = {"src": "new-src", "inc": "new-inc"}
    plans = [
        {"id": "p1", "source_optimization_id": "src", "incumbent_optimization_id": "inc", "status": "active"},
        {"id": "p2", "source_optimization_id": "src", "incumbent_optimization_id": None, "status": "ended"},
        {"id": "p3", "source_optimization_id": "other", "incumbent_optimization_id": "missing", "status": "active"},
        {"id": "p4", "source_optimization_id": "inc", "incumbent_optimization_id": None, "status": "paused"},
    ]
    moves = {move["plan_id"]: move for move in plan_moves(plans, converted)}
    assert moves["p1"]["action"] == "move"
    assert moves["p1"]["from_job"] == "inc"
    assert moves["p1"]["new_run"] == "new-inc"
    assert moves["p2"]["action"] == "skip"
    assert "ended" in moves["p2"]["reason"]
    assert moves["p3"]["action"] == "skip"
    assert "missing" in moves["p3"]["reason"]
    assert moves["p4"]["action"] == "skip"
    assert "Another plan" in moves["p4"]["reason"]
    sql = plan_move_sql(list(moves.values()))
    assert sql.count("UPDATE continuous_plans") == 1
    assert "WHERE id = 'p1' AND source_optimization_id = 'src'" in sql
    assert '{"incumbent": "new-inc", "tried_models": []}' in sql
    assert plan_move_sql([]) == "-- No continuous plan moves.\n"
    assert "status = 'active'" not in sql


def test_plan_ended_as_dspy_retired_moves_and_restarts() -> None:
    """Move a plan the worker ended for its DSPy run, and make it active again."""
    plans = [
        {
            "id": "p1",
            "source_optimization_id": "src",
            "incumbent_optimization_id": None,
            "status": "ended",
            "pause_reason": "dspy_retired",
        },
        {
            "id": "p2",
            "source_optimization_id": "other",
            "incumbent_optimization_id": None,
            "status": "ended",
            "pause_reason": "user",
        },
    ]
    moves = {move["plan_id"]: move for move in plan_moves(plans, {"src": "new-src", "other": "new-other"})}
    assert moves["p1"]["action"] == "move"
    assert moves["p1"]["restart"] is True
    assert moves["p2"]["action"] == "skip"
    sql = plan_move_sql(list(moves.values()))
    assert sql.count("UPDATE continuous_plans") == 1
    assert (
        "SET status = 'active', pause_reason = NULL, next_check_at = now(), source_optimization_id = 'new-src'" in sql
    )


def test_generated_serve_and_scorer_run_in_a_subprocess(tmp_path: Path) -> None:
    """Run the generated serve.py and scorer.py on a stub program that calls no model."""
    job = make_job()
    job["payload"]["task"]["artifact"] = {"kind": "text", "starting_version": "Start."}
    job["payload"]["workspace"]["run_code"] = TEXT_RUN_CODE
    job["result"] = {"best_candidate": "Be brief."}
    repo = write_repo(tmp_path / "repo", convert_job(job)["files"])

    served = run_python(repo, "import serve; print(json.dumps(serve.serve({'question': 'q1'})))")
    assert served == {"answer": "Be brief.|q1"}
    case = {"input": {"question": "q1"}, "expected": "q1"}
    scored = run_python(repo, f"import scorer; print(json.dumps(scorer.score({str(repo)!r}, {case!r})))")
    assert scored == {"score": 1.0, "feedback": "match", "scores": {"exact": {"score": 1.0, "feedback": "exact: 1"}}}


def test_generated_dspy_program_applies_instructions(tmp_path: Path) -> None:
    """Apply prompts.json to the DSPy predictor and return a JSON-safe Prediction from serve()."""
    pytest.importorskip("dspy")
    repo = write_repo(tmp_path / "repo", convert_job(make_job())["files"])
    served = run_python(repo, "import serve; print(json.dumps(serve.serve({'question': 'q2'})))")
    assert served == {"answer": "Answer briefly.|q2"}
    case = {"input": {"question": "q2"}, "expected": "zzz"}
    scored = run_python(repo, f"import scorer; print(json.dumps(scorer.score({str(repo)!r}, {case!r})))")
    assert scored["score"] == 0.0
    assert scored["feedback"] == "no match"


def _no_network(*_args: Any, **_kwargs: Any) -> None:
    """Fail any socket connection a test makes."""
    raise AssertionError("network access attempted")


def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
    """Fail when the dry run reaches GitHub."""
    raise AssertionError("GitHub reached")


def test_dry_run_writes_files_and_opens_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write repo files, request and report for each job, with no network and no GitHub call."""
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    legacy = {"optimization_id": "old-1", "username": "bob", "optimization_type": "run", "payload": {}}
    plans = [{"id": "p1", "source_optimization_id": "job-0001-aaaa", "status": "active"}]
    out = tmp_path / "out"
    code = main(
        ["--out", str(out), "--username", "alice"],
        engine_factory=object,
        job_loader=lambda _engine, users, ids: [make_job(), legacy] if users == ["alice"] and not ids else [],
        plan_loader=lambda _engine, ids: plans if ids == ["job-0001-aaaa"] else [],
        token_for=_forbidden,
        create_repo=_forbidden,
    )
    assert code == 0
    folder = out / "job-0001-aaaa"
    assert (folder / "repo" / "program" / "__init__.py").is_file()
    assert (folder / "repo" / "scorer.py").is_file()
    request = json.loads((folder / "request.json").read_text())
    BlackboxRunRequest.model_validate(request)
    report = json.loads((folder / "report.json").read_text())
    assert report["repository"] is None
    summary = json.loads((out / "summary.json").read_text())
    assert [s["job_id"] for s in summary["skipped"]] == ["old-1"]
    assert summary["plan_moves"][0]["action"] == "move"
    assert "<new run id for job-0001-aaaa>" in (out / "plan_moves.sql").read_text()


def test_apply_refuses_without_confirm_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse --apply without, or with a wrong, --confirm-environment, before any database read."""
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    assert main(["--out", str(tmp_path), "--apply"], engine_factory=_forbidden) == 2
    assert main(["--out", str(tmp_path), "--apply", "--confirm-environment", "prod"], engine_factory=_forbidden) == 2


class _Created:
    """Stub of ``CreatedRepository``."""

    full_name = "alice/skynet-support-qa-job-0001"
    default_branch = "main"
    commit = "a" * 40


def test_apply_creates_repo_and_prints_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Create the repository through the stub, pin its commit in the request, and print the SQL."""
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    calls: list[dict[str, Any]] = []

    def create(token: str, name: str, files: dict[str, str], **kwargs: Any) -> _Created:
        """Record one repository creation."""
        calls.append({"token": token, "name": name, "files": files, **kwargs})
        return _Created()

    plans = [{"id": "p1", "source_optimization_id": "job-0001-aaaa", "status": "active"}]
    code = main(
        ["--out", str(tmp_path), "--apply", "--confirm-environment", "staging"],
        engine_factory=object,
        job_loader=lambda *_: [make_job()],
        plan_loader=lambda *_: plans,
        token_for=lambda _engine, user: f"token-for-{user}",
        create_repo=create,
    )
    assert code == 0
    assert len(calls) == 1
    assert calls[0]["private"] is True
    assert calls[0]["name"] == "skynet-support-qa-job-0001"
    request = json.loads((tmp_path / "job-0001-aaaa" / "request.json").read_text())
    assert request["target"]["repo"]["repository"] == _Created.full_name
    assert request["target"]["repo"]["commit"] == _Created.commit
    assert request["target"]["repo"]["created_by_skynet"] is True
    BlackboxRunRequest.model_validate(request)
    printed = capsys.readouterr().out
    assert "UPDATE continuous_plans" in printed
    assert "token-for-alice" not in printed


def _conflict(*_args: Any, **_kwargs: Any) -> Any:
    """Fail like GitHub does when the repository name is taken."""
    raise RepoCreateError("Your GitHub account already has a repository named 'x'. Choose another name.")


def _apply(tmp_path: Path, reuse: Any) -> dict[str, Any]:
    """Run ``--apply`` for one job whose repository name is taken, and return its report.

    Args:
        tmp_path: Output folder.
        reuse: Stub for ``reuse_repo``.

    Returns:
        The job's ``report.json`` and ``request.json`` contents.
    """
    code = main(
        ["--out", str(tmp_path), "--apply", "--confirm-environment", "staging"],
        engine_factory=object,
        job_loader=lambda *_: [make_job()],
        plan_loader=lambda *_: [],
        token_for=lambda _engine, user: f"token-for-{user}",
        create_repo=_conflict,
        reuse_repo=reuse,
    )
    assert code == 0
    folder = tmp_path / "job-0001-aaaa"
    return {
        "report": json.loads((folder / "report.json").read_text()),
        "request": json.loads((folder / "request.json").read_text()),
    }


def test_apply_rerun_reuses_the_repository_of_the_first_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A name conflict on a rerun pins the repository the first run created, not the placeholder."""
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    calls: list[str] = []

    def reuse(token: str, name: str, files: dict[str, str], *, message: str) -> _Created:
        """Return the repository an earlier run created."""
        calls.append(name)
        return _Created()

    result = _apply(tmp_path, reuse)
    assert calls == ["skynet-support-qa-job-0001"]
    repo = result["request"]["target"]["repo"]
    assert repo["repository"] == _Created.full_name
    assert repo["commit"] == _Created.commit
    assert repo["created_by_skynet"] is True
    assert result["report"]["repository"] == _Created.full_name


def test_apply_reports_conflict_when_no_repository_to_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the placeholder and report the failure when the conflict has no repository behind it."""
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    result = _apply(tmp_path, lambda *_args, **_kwargs: None)
    assert result["request"]["target"]["repo"]["repository"] == PLACEHOLDER_REPOSITORY
    assert result["report"]["repository"] is None
    assert any("already has a repository" in w for w in result["report"]["warnings"])


def test_model_credentials_never_reach_the_request() -> None:
    """Stored keys, tokens and scoped routes stay out of the converted run request."""
    job = make_job()
    job["payload"]["models"]["reflection_model"] = {
        "name": "openai/gpt-4o",
        "temperature": 0.2,
        "api_key": "sk-test-not-real",
        "extra": {"api_key": "sk-test-not-real", "scoped_route": {"token": "t"}, "reasoning_effort": "low"},
    }
    request = convert_job(job)["request"]
    reflection = request["reflection_model_config"]
    assert reflection == {"name": "openai/gpt-4o", "temperature": 0.2, "extra": {"reasoning_effort": "low"}}
    assert "sk-test-not-real" not in json.dumps(request)

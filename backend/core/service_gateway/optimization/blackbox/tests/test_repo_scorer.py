"""Tests for scoring repository versions in the parent-owned box."""

from __future__ import annotations

import base64
import http.server
import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from core.billing.budgets import BudgetError, BudgetInsufficientError
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import UsagePendingError
from core.billing.signals import BudgetReached
from core.billing.usage_tags import usage_scope
from core.exceptions import ServiceError
from core.models.blackbox import BlackboxRunRequest
from core.models.tests.test_blackbox import _repo_request
from core.worker.scoped_relay import CallRecorder

from .. import repo_scorer, runner
from ..repo_constraints import McpServer, RoleEnvironment, ServiceSpec, TargetStage
from ..repo_scorer import (
    _STAGE_LAYOUT_SECONDS,
    SETUP_ALLOWANCE_SECONDS,
    RepoScoreError,
    RepoScorer,
    _scrub,
    _StageRelay,
    fold_case_scores,
)
from ..repo_workspace import PREPARED_DIR, RepoWorkspace
from ..rule_judge import (
    EVENT_BLOCK,
    EVENT_NOTE,
    EVENT_VERDICT,
    ILLEGAL_ACTION_MESSAGE,
    WATCH_EVERY_ACTIONS,
    RuleJudge,
)
from ..sandbox import CommandResult, LocalSubprocessRuntime, SandboxSpec
from ..sandbox_contract import SandboxSession
from ..service import run_blackbox_optimization
from .test_repo_workspace import _archive, _patch

SCORER = """
import os

def score(repo_path, case=None):
    value = int(open(os.path.join(repo_path, "src/app.py")).read().split("=")[1])
    built = open(os.path.join(repo_path, "built.txt")).read()
    return {
        "score": value / 10,
        "feedback": "value read",
        "secret": os.environ.get("API_KEY"),
        "built": built,
        "case": case,
        "cases": {
            "small": {"score": value / 20, "feedback": "small ok"},
            "large": {"score": value / 5, "feedback": "large ok"},
        },
    }
"""


@pytest.fixture(autouse=True)
def _free_relay_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let each test's runner bind a free port, so parallel workers never share the fixed one.

    Args:
        monkeypatch: Restores the fixed port after the test.
    """
    monkeypatch.setattr(runner, "RELAY_PORT", 0)


@pytest.fixture
def scored(tmp_path: Path) -> Iterator[tuple[RepoScorer, Path, Path]]:
    """Open a scorer over a small tree whose setup builds a file.

    Args:
        tmp_path: Scratch folder.

    Yields:
        The scorer, the archive and the scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "README": "r\n"})
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=120, network_disabled=True),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command="cat src/app.py > built.txt",
            secrets={"API_KEY": "very-secret-value"},
        ),
        SCORER,
        timeout_seconds=60,
    )
    yield scorer, archive, tmp_path
    scorer.close()


def test_scores_the_baseline_and_a_version(scored: tuple[RepoScorer, Path, Path]) -> None:
    """The empty patch scores the commit; a patch scores its own checkout after setup."""
    scorer, archive, root = scored
    baseline = scorer.score("")
    assert baseline["score"] == pytest.approx(0.1)
    assert baseline["built"] == "x = 1\n"
    improved = scorer.score(_patch(root / "v1", archive, {"src/app.py": "x = 7\n"}), {"id": 3})
    assert improved["score"] == pytest.approx(0.7)
    assert improved["case"] == {"id": 3}
    assert "cases" not in improved
    assert improved["scores"] == {
        "small": {"score": pytest.approx(0.35), "feedback": "small ok"},
        "large": {"score": pytest.approx(1.4), "feedback": "large ok"},
    }


def test_no_run_secret_reaches_the_scorer(scored: tuple[RepoScorer, Path, Path]) -> None:
    """The box env holds no run secret; listed services get keys only through the relay."""
    scorer, _, _ = scored
    assert scorer.score("")["secret"] is None


def test_versions_outside_the_editable_paths_are_refused(scored: tuple[RepoScorer, Path, Path]) -> None:
    """A change to a file the run may not edit is reported, not scored."""
    scorer, archive, root = scored
    with pytest.raises(RepoScoreError, match="README"):
        scorer.score(_patch(root / "bad", archive, {"README": "changed\n"}))


def test_a_failing_scorer_is_reported(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A scorer that raises gives an unscorable version and logs its error on the server."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=60, network_disabled=True),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
        ),
        "def score(repo_path):\n    raise RuntimeError('tests failed')\n",
        timeout_seconds=60,
    )
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(RepoScoreError, match="raised an error"):
            scorer.score("")
        assert "tests failed" in caplog.text
    finally:
        scorer.close()


def test_a_version_whose_code_rewrites_its_judge_gets_no_score(tmp_path: Path) -> None:
    """Refuse the score of a source-only version that overwrites the protected labels while it runs."""
    archive = _archive(tmp_path, {"src/app.py": "pass\n", "eval/labels.csv": "1\n"})
    judge = (
        "import os, runpy\n"
        "def score(repo_path):\n"
        "    runpy.run_path(os.path.join(repo_path, 'src/app.py'))\n"
        "    return {'score': float(open(os.path.join(repo_path, 'eval/labels.csv')).read()), 'feedback': 'labels read'}\n"
    )
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=120, network_disabled=True),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
            protected_paths=["eval"],
        ),
        judge,
        timeout_seconds=60,
    )
    try:
        assert scorer.score("")["score"] == pytest.approx(1.0)
        cheat = _patch(tmp_path / "cheat", archive, {"src/app.py": "open('eval/labels.csv', 'w').write('99')\n"})
        refused = scorer.dispatch({"candidate": cheat})
        assert refused.status == 422
        assert "'eval/labels.csv' is protected" in refused.body.decode()
        assert scorer.score("")["score"] == pytest.approx(1.0)
    finally:
        scorer.close()


def _labels_judge(tmp_path: Path, secrets: dict[str, str]) -> tuple[RepoScorer, Path]:
    """Build a scorer whose judge runs the version's source, then reads the protected labels.

    Args:
        tmp_path: Scratch folder.
        secrets: Environment the scorer receives.

    Returns:
        The scorer and its archive.
    """
    archive = _archive(tmp_path, {"src/app.py": "pass\n", "eval/labels.csv": "1\n"})
    judge = (
        "import os, runpy\n"
        "def score(repo_path):\n"
        "    runpy.run_path(os.path.join(repo_path, 'src/app.py'))\n"
        "    return {'score': float(open(os.path.join(repo_path, 'eval/labels.csv')).read()), 'feedback': 'labels read'}\n"
    )
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=120, network_disabled=True),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets=secrets,
            protected_paths=["eval"],
        ),
        judge,
        timeout_seconds=60,
    )
    return scorer, archive


def test_a_version_that_restores_its_judge_at_exit_gets_no_score(tmp_path: Path) -> None:
    """Refuse a version that rewrites the labels for the judge and puts them back when the process exits."""
    scorer, archive = _labels_judge(tmp_path, {})
    cheat = (
        "import atexit\n"
        "saved = open('eval/labels.csv').read()\n"
        "open('eval/labels.csv', 'w').write('99')\n"
        "atexit.register(lambda: open('eval/labels.csv', 'w').write(saved))\n"
    )
    try:
        refused = scorer.dispatch({"candidate": _patch(tmp_path / "cheat", archive, {"src/app.py": cheat})})
        assert refused.status == 422
        assert "'eval/labels.csv' is protected" in refused.body.decode()
        assert scorer.score("")["score"] == pytest.approx(1.0)
    finally:
        scorer.close()


def test_a_protected_file_named_after_a_secret_is_reported_redacted(tmp_path: Path) -> None:
    """Hide a secret the version's code writes into a protected file name, as everywhere else."""
    scorer, archive = _labels_judge(tmp_path, {"API_KEY": "very-secret-value"})
    # The box env holds no secret, so the version names the file after a value it guessed.
    leak = "import os\nopen(os.path.join('eval', 'very-secret-value'), 'w').close()\n"
    try:
        refused = scorer.dispatch({"candidate": _patch(tmp_path / "leak", archive, {"src/app.py": leak})})
        assert refused.status == 422
        assert "very-secret-value" not in refused.body.decode()
        assert "'eval/[secret]' is protected" in refused.body.decode()
    finally:
        scorer.close()


class _WriteTouchingSession:
    """Local box whose file writes move every file's change time, as a Vercel box's do."""

    def __init__(self, session: Any) -> None:
        """Wrap one local box.

        Args:
            session: The local box.
        """
        self._session = session

    def __getattr__(self, name: str) -> Any:
        """Pass everything else to the local box.

        Args:
            name: The attribute asked for.

        Returns:
            The local box's attribute.
        """
        return getattr(self._session, name)

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write the files, then touch the metadata of everything in the box.

        Args:
            files: Relative path to content.
        """
        self._session.write_files(files)
        # Flip a bit and back: macOS keeps the change time when the mode is unchanged.
        self._session.run("chmod -R o+w . && chmod -R o-w .")


class _WriteTouchingRuntime(LocalSubprocessRuntime):
    """Local runtime whose boxes behave like Vercel's on file writes."""

    def open(self, spec: SandboxSpec) -> Any:
        """Open a local box wrapped so writes touch every file.

        Args:
            spec: The box's spec.

        Returns:
            The wrapped box.
        """
        return _WriteTouchingSession(super().open(spec))


def test_a_clean_version_keeps_its_score_when_box_writes_touch_every_file(tmp_path: Path) -> None:
    """Stamp the protected files after the host's last write, so a box that touches files on write flags nothing."""
    archive = _archive(tmp_path, {"src/app.py": "pass\n", "eval/labels.csv": "1\n"})
    judge = (
        "import os\n"
        "def score(repo_path):\n"
        "    return {'score': float(open(os.path.join(repo_path, 'eval/labels.csv')).read()), 'feedback': 'read'}\n"
    )
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=_WriteTouchingRuntime(),
            spec=SandboxSpec(lifetime_seconds=120, network_disabled=True),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
            protected_paths=["eval"],
        ),
        judge,
        timeout_seconds=60,
    )
    try:
        assert scorer.score("")["score"] == pytest.approx(1.0)
        assert scorer.score("")["score"] == pytest.approx(1.0)
    finally:
        scorer.close()


def test_a_judge_that_imports_its_protected_package_keeps_its_score(tmp_path: Path) -> None:
    """Score a version whose judge imports a protected package, which must not leave bytecode there."""
    archive = _archive(
        tmp_path,
        {"src/app.py": "pass\n", "eval/__init__.py": "", "eval/metric.py": "def value():\n    return 0.5\n"},
    )
    judge = (
        "import os, sys\n"
        "def score(repo_path):\n"
        "    sys.path.insert(0, repo_path)\n"
        "    from eval.metric import value\n"
        "    return {'score': value(), 'feedback': 'judged'}\n"
    )
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            # A Vercel box writes bytecode by default; the local runtime does not.
            spec=SandboxSpec(lifetime_seconds=120, network_disabled=True, env={"PYTHONDONTWRITEBYTECODE": ""}),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
            protected_paths=["eval"],
        ),
        judge,
        timeout_seconds=60,
    )
    try:
        assert scorer.score("")["score"] == pytest.approx(0.5)
        assert scorer.score("")["score"] == pytest.approx(0.5)
    finally:
        scorer.close()


def test_dispatch_answers_the_guest(scored: tuple[RepoScorer, Path, Path]) -> None:
    """The evaluator capability returns the score, or 422 with the reason."""
    scorer, archive, root = scored
    ok = scorer.dispatch({"candidate": "", "case": None})
    assert ok.status == 200
    assert json.loads(ok.body)["score"] == pytest.approx(0.1)
    refused = scorer.dispatch({"candidate": _patch(root / "bad", archive, {"README": "x\n"})})
    assert refused.status == 422
    assert "README" in refused.body.decode()
    with pytest.raises(ValueError):
        scorer.dispatch({"candidate": {"a": "b"}})


@pytest.mark.parametrize(
    ("side_info", "expected"),
    [
        ({"feedback": "f"}, {"feedback": "f"}),
        (
            {"feedback": "f", "cases": {"a": {"score": 1, "feedback": "x"}, "b": (0.5, "y")}},
            {"feedback": "f", "scores": {"a": {"score": 1.0, "feedback": "x"}, "b": {"score": 0.5, "feedback": "y"}}},
        ),
    ],
)
def test_fold_case_scores(side_info: dict, expected: dict) -> None:
    """Per-case scores are optional and become named scores, each with its feedback."""
    assert fold_case_scores(side_info) == expected


@pytest.mark.parametrize(
    "cases",
    [[1, 2], {"a": "high"}, {"a": {"score": float("nan"), "feedback": "x"}}, {"a": True}, {"a": 1.0}],
)
def test_fold_case_scores_reject_bad_shapes(cases: object) -> None:
    """Per-case scores must map names to a finite score with its own feedback."""
    with pytest.raises(RepoScoreError):
        fold_case_scores({"cases": cases})


@pytest.mark.parametrize("engine", ["autoresearch", "gepa"])
def test_a_repository_run_needs_its_tree(engine: str) -> None:
    """Refuse a repository run the guest cannot carry out before any scoring starts."""
    payload = BlackboxRunRequest.model_validate(
        _repo_request(max_cost_cents=100, strategy={"mode": "single", "engine": engine})
    )
    route = {"url": "http://127.0.0.1:1/v1/_evaluator", "token": "t"}
    with pytest.raises(ServiceError, match="repository its parent fetched"):
        run_blackbox_optimization(payload, artifact_id="job", evaluator_route=route, repo_snapshot=None)


class _StubJudge:
    """Answer watch hook prompts with one fixed reply and count the questions."""

    def __init__(self, reply: str | None) -> None:
        """Keep the reply.

        Args:
            reply: What the hook model answers, or ``None`` for a failed call.
        """
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str | None:
        """Return the fixed reply.

        Args:
            prompt: The watch prompt.

        Returns:
            The reply.
        """
        self.prompts.append(prompt)
        return self.reply


_LEGAL = json.dumps({"label": "ok", "confidence": 0.9, "reason": "General change."})
_HARDCODED = json.dumps({"label": "breaks_rule", "confidence": 0.9, "reason": "It reads expected answers."})


def _ruled(tmp_path: Path, hook: _StubJudge | None = None) -> tuple[RepoScorer, Path, list[dict[str, Any]], RuleJudge]:
    """Open a scorer for a repository with one plain-language rule and a protected folder.

    Args:
        tmp_path: Scratch folder.
        hook: The watch hook stub.

    Returns:
        The scorer, the archive, the recorded rule events and the judge.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "eval/labels.csv": "1\n"})
    events: list[dict[str, Any]] = []
    judge = RuleJudge(
        ("Do not hardcode answers for dataset cases.",),
        intent="Raise the value in a general way.",
        ask=hook or _StubJudge(_LEGAL),
        emit=events.append,
        run_watch=lambda watch: watch(),
    )
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=120, network_disabled=True),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
            protected_paths=["eval"],
        ),
        "import os\ndef score(repo_path):\n"
        "    value = int(open(os.path.join(repo_path, 'src/app.py')).read().split('=')[1])\n"
        "    return {'score': value / 10, 'feedback': 'value read'}\n",
        timeout_seconds=60,
        rule_judge=judge,
    )
    return scorer, archive, events, judge


def test_a_blocked_version_gets_only_the_fixed_message(tmp_path: Path) -> None:
    """A path-rule refusal tells the agent the fixed text; the reason goes to the run's events."""
    scorer, archive, events, _ = _ruled(tmp_path)
    try:
        reply = scorer.dispatch({"candidate": _patch(tmp_path / "bad", archive, {"eval/labels.csv": "9\n"})})
        assert reply.status == 422
        assert reply.body.decode() == ILLEGAL_ACTION_MESSAGE
        assert events == [
            {
                "kind": EVENT_BLOCK,
                "rule": "optimize.toml [paths]",
                "reason": "'eval/labels.csv' is protected by the repository rules ([paths] protected).",
            }
        ]
    finally:
        scorer.close()


def test_a_version_that_rewrites_a_protected_file_while_it_runs_is_blocked(tmp_path: Path) -> None:
    """The runtime protected-file check is the same gate: fixed message, reason in the events."""
    scorer, archive, events, _ = _ruled(tmp_path)
    try:
        cheat = _patch(
            tmp_path / "cheat",
            archive,
            {"src/app.py": "x = 1\nopen('eval/labels.csv', 'w').write('99')\n"},
        )
        scorer._code = "import os, runpy\ndef score(repo_path):\n    runpy.run_path(os.path.join(repo_path, 'src/app.py'))\n    return {'score': 1.0}\n"
        reply = scorer.dispatch({"candidate": cheat})
        assert reply.status == 422
        assert reply.body.decode() == ILLEGAL_ACTION_MESSAGE
        assert [event["kind"] for event in events] == [EVENT_BLOCK]
        assert "eval/labels.csv" in events[0]["reason"]
    finally:
        scorer.close()


def test_a_version_the_rules_name_is_scored_like_any_other(tmp_path: Path) -> None:
    """With the final judge gone, scoring never asks the hook and never scores a version 0 for a rule."""
    hook = _StubJudge(_HARDCODED)
    scorer, archive, events, _ = _ruled(tmp_path, hook)
    try:
        assert scorer.score("")["score"] == pytest.approx(0.1)
        reply = scorer.dispatch({"candidate": _patch(tmp_path / "v1", archive, {"src/app.py": "x = 9\n"})})
        assert reply.status == 200
        assert json.loads(reply.body) == {"score": pytest.approx(0.9), "feedback": "value read"}
        assert hook.prompts == []
        assert events == []
    finally:
        scorer.close()


def test_a_watch_note_reaches_the_agent_in_its_next_scoring_reply(tmp_path: Path) -> None:
    """The watch judge's warning is put in front of the next feedback the agent reads, once."""
    scorer, archive, events, judge = _ruled(tmp_path, _StubJudge(_HARDCODED))
    try:
        for index in range(WATCH_EVERY_ACTIONS):
            judge.record_action(f"action {index}")
        version = _patch(tmp_path / "v1", archive, {"src/app.py": "x = 4\n"})
        warned = json.loads(scorer.dispatch({"candidate": version}).body)
        assert warned["feedback"] == (
            "Check the rules in your brief. You are probably about to break one of them:"
            " It reads expected answers.\n\nvalue read"
        )
        assert warned["score"] == pytest.approx(0.4)
        assert json.loads(scorer.dispatch({"candidate": version}).body)["feedback"] == "value read"
        assert [event["kind"] for event in events] == [EVENT_VERDICT, EVENT_NOTE]
    finally:
        scorer.close()


def _leaky(tmp_path: Path, code: str, rule_judge: RuleJudge | None = None) -> RepoScorer:
    """Open a scorer whose metric names its own files and internals.

    Args:
        tmp_path: Scratch folder.
        code: Scorer source.
        rule_judge: The run's rule judge, if any.

    Returns:
        The scorer.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "eval/metric.py": "WEIGHT = 0.3\n"})
    return RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=60, network_disabled=True),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
            hidden_paths=["eval"],
        ),
        code,
        timeout_seconds=60,
        rule_judge=rule_judge,
    )


def test_the_proposer_gets_only_the_score_and_the_feedback(tmp_path: Path) -> None:
    """Extra keys, the box's paths and each case's extras never reach the agent (SKY-353)."""
    scorer = _leaky(
        tmp_path,
        "import os\ndef score(repo_path):\n"
        "    return {'score': 0.5, 'feedback': 'read ' + os.path.join(repo_path, 'eval/metric.py'),"
        " 'weights': {'accuracy': 0.3}, 'metric_source': open(os.path.join(repo_path, 'eval/metric.py')).read(),"
        " 'cases': {'a': {'score': 1, 'feedback': 'at ' + repo_path, 'formula': 'w * x'}}}\n",
    )
    try:
        reply = scorer.dispatch({"candidate": ""})
        assert reply.status == 200
        document = json.loads(reply.body)
        assert document == {
            "score": pytest.approx(0.5),
            "feedback": "read eval/metric.py",
            "scores": {"a": {"score": pytest.approx(1.0), "feedback": "at ."}},
        }
    finally:
        scorer.close()


def test_a_crashing_metric_sends_no_traceback_or_box_path(tmp_path: Path) -> None:
    """A scorer crash tells the agent only that the scorer failed, not its paths or lines (SKY-353)."""
    scorer = _leaky(
        tmp_path,
        "import os\ndef score(repo_path):\n    open(os.path.join(repo_path, 'eval/missing.csv'))\n",
    )
    try:
        reply = scorer.dispatch({"candidate": ""})
        assert reply.status == 422
        text = reply.body.decode()
        assert "eval/missing.csv" not in text
        assert "Traceback" not in text
        assert 'File "' not in text
        assert "os.path.join" not in text
        assert ".app-repo" not in text
        assert str(tmp_path) not in text
    finally:
        scorer.close()


def test_a_write_under_the_hidden_metric_skips_the_rule_judge(tmp_path: Path) -> None:
    """A hidden-file change gets the neutral 422, not the judge's message or a rule.block event (SKY-353)."""
    events: list[dict[str, Any]] = []
    judge = RuleJudge(
        ("Do not hardcode answers for dataset cases.",),
        intent="Raise the value in a general way.",
        ask=_StubJudge(_LEGAL),
        emit=events.append,
        run_watch=lambda watch: watch(),
    )
    scorer = _leaky(
        tmp_path,
        "import os\ndef score(repo_path):\n"
        "    open(os.path.join(repo_path, 'eval/cache.txt'), 'w').write('1')\n"
        "    return {'score': 1.0}\n",
        rule_judge=judge,
    )
    try:
        reply = scorer.dispatch({"candidate": ""})
        assert reply.status == 422
        assert reply.body.decode() == "The metric files changed while the version ran."
        assert events == []
    finally:
        scorer.close()


@pytest.mark.parametrize("score", ["0.5 WEIGHT = 0.3", [0.3], {"w": 0.3}], ids=["text", "list", "object"])
def test_a_score_that_is_not_a_number_gets_a_fixed_422(tmp_path: Path, score: Any) -> None:
    """The 422 never echoes a score value that the version's code can set (SKY-353).

    The runner checks the score, so the code rewrites the output file after the runner writes it.
    """
    forged = json.dumps({"score": score})
    scorer = _leaky(
        tmp_path,
        "import atexit, os, sys\n"
        f"atexit.register(lambda: open(os.path.join(sys.argv[1], 'output.json'), 'w').write({forged!r}))\n"
        "def score(repo_path):\n    return {'score': 1.0, 'feedback': 'ok'}\n",
    )
    try:
        reply = scorer.dispatch({"candidate": ""})
        assert reply.status == 422
        assert reply.body.decode() == "The scorer returned a score that is not a number."
    finally:
        scorer.close()


def test_a_version_that_rewrites_the_metric_gets_the_base_score(tmp_path: Path) -> None:
    """The scorer box keeps the base commit's metric, so editing it changes nothing (SKY-353)."""
    scorer = _leaky(
        tmp_path,
        "import os, runpy\ndef score(repo_path):\n"
        "    weight = runpy.run_path(os.path.join(repo_path, 'eval/metric.py'))['WEIGHT']\n"
        "    return {'score': weight, 'feedback': 'ok'}\n",
    )
    try:
        archive = tmp_path / "tree.tgz"
        baseline = json.loads(scorer.dispatch({"candidate": ""}).body)["score"]
        cheat = _patch(tmp_path / "cheat", archive, {"src/app.py": "x = 2\n", "eval/metric.py": "WEIGHT = 99\n"})
        reply = scorer.dispatch({"candidate": cheat})
        assert reply.status == 200
        assert json.loads(reply.body)["score"] == pytest.approx(baseline) == pytest.approx(0.3)
    finally:
        scorer.close()


@pytest.mark.parametrize(
    "leak",
    [
        "import sys\nsys.stderr.write(SOURCE)\nsys.stderr.flush()\nos._exit(3)\n",
        "raise RuntimeError(SOURCE)\n",
    ],
    ids=["stderr-and-exit", "exception-text"],
)
def test_a_version_that_reads_the_hidden_metric_gets_a_422_without_it(
    tmp_path: Path, leak: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Version code that prints or raises a hidden file's content sends none of it to the guest (SKY-353)."""
    scorer = _leaky(
        tmp_path,
        "import os, runpy\ndef score(repo_path):\n"
        "    runpy.run_path(os.path.join(repo_path, 'src/app.py'))\n"
        "    return {'score': 1.0, 'feedback': 'ok'}\n",
    )
    try:
        cheat = _patch(
            tmp_path / "cheat",
            tmp_path / "tree.tgz",
            {"src/app.py": f"import os\nSOURCE = open('eval/metric.py').read()\n{leak}"},
        )
        with caplog.at_level(logging.WARNING):
            reply = scorer.dispatch({"candidate": cheat})
        assert reply.status == 422
        assert "WEIGHT" not in reply.body.decode()
        assert "WEIGHT = 0.3" in caplog.text
    finally:
        scorer.close()


def _grouped_failure() -> None:
    """Raise an exception group that holds a nested group, as a metric might."""
    try:
        {}["missing"]  # METRIC_LINE_ONE
    except KeyError as error:
        inner = ExceptionGroup("inner", [ValueError("bad")])
        raise ExceptionGroup("checks failed", [error, inner]) from None  # METRIC_LINE_TWO


def test_an_exception_group_traceback_sends_no_frame_or_source_line() -> None:
    """Exception group frames sit behind ``|`` margins and must go like plain frames (SKY-353)."""
    try:
        _grouped_failure()
    except ExceptionGroup as group:
        text = "".join(traceback.format_exception(group))

    scrubbed = _scrub(text, None)

    assert "  |   File " in text
    assert "METRIC_LINE_TWO" in text
    assert "METRIC_LINE" not in scrubbed
    assert 'File "' not in scrubbed
    assert "Traceback" not in scrubbed
    assert "ExceptionGroup: checks failed (2 sub-exceptions)" in scrubbed
    assert "ValueError: bad" in scrubbed


STAGE_TREE = {
    "src/app.py": "x = 1\n",
    "src/stages.py": (
        "import time\n"
        "FACTOR = 2\n"
        "def double(value):\n"
        "    return {'v': value['v'] * FACTOR}\n"
        "def broken(value):\n"
        "    raise RuntimeError('stage broke')\n"
        "def slow(value):\n"
        "    time.sleep(value)\n"
        "    return value\n"
    ),
}
STAGES = {
    "double": TargetStage(entry="src/stages.py:double", timeout=30),
    "shout": TargetStage(
        entry=("python3", "-c", "import json, sys; print(json.dumps(json.load(sys.stdin).upper()))"), timeout=30
    ),
    "broken": TargetStage(entry="src/stages.py:broken", timeout=30),
    "slow": TargetStage(entry="src/stages.py:slow", timeout=1),
}


class RecordingRuntime:
    """Open local boxes and remember each spec and each close."""

    injects_headers = False

    def __init__(self, fail_on: str | None = None, error: Exception | None = None) -> None:
        """Set which stage box refuses to open.

        Args:
            fail_on: The stage name whose box raises ``error`` on open.
            error: What the refused open raises.
        """
        self.specs: list[SandboxSpec] = []
        self.sessions: list[SandboxSession] = []
        self.closed = 0
        self._fail_on = fail_on
        self._error = error or RuntimeError("no capacity")

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Open one local box, or refuse the configured stage.

        Args:
            spec: The stage box's spec.

        Returns:
            The local session, which counts its close.

        Raises:
            Exception: The configured error for the refused stage.
        """
        if self._fail_on and spec.operation_key.endswith(f":{self._fail_on}"):
            raise self._error
        self.specs.append(spec)
        session = LocalSubprocessRuntime().open(spec)
        close = session.close

        def counted_close() -> None:
            """Count the close, then remove the box."""
            self.closed += 1
            close()

        session.close = counted_close  # type: ignore[method-assign]
        self.sessions.append(session)
        return session


def _stage_scorer(
    tmp_path: Path,
    code: str,
    runtime: Any,
    stages: Mapping[str, TargetStage] = STAGES,
    *,
    scorer_runtime: Any = None,
    protected_paths: tuple[str, ...] = (),
    **kwargs: Any,
) -> tuple[RepoScorer, Path]:
    """Open a scorer whose run has target stages.

    Args:
        tmp_path: Scratch folder.
        code: The scorer code.
        runtime: Opens the stage boxes.
        stages: The run's stages.
        scorer_runtime: Opens the scorer's box; a plain local box when unset.
        protected_paths: Paths the version's code must not change while it runs.
        **kwargs: More ``RepoScorer`` options.

    Returns:
        The scorer and the tree's archive.
    """
    archive = _archive(tmp_path, {**STAGE_TREE, "eval/metric.py": "hidden\n", "judge/labels.csv": "1\n"})
    stage_archive = _archive(tmp_path / "stage", STAGE_TREE)
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=scorer_runtime or LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=120, network_disabled=True),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
            protected_paths=list(protected_paths),
        ),
        code,
        timeout_seconds=60,
        stages=stages,
        stage_runtime=runtime,
        stage_archive=stage_archive,
        hidden_paths=("eval/metric.py",),
        **kwargs,
    )
    return scorer, archive


def test_one_and_two_stages_answer_and_see_the_version(tmp_path: Path) -> None:
    """The scorer calls two stages; each stage box runs the scored version, and every box closes."""
    code = (
        "from skynet import call\n"
        "def score(repo_path, case=None):\n"
        "    doubled = call('double', {'v': 3})\n"
        "    return {'score': doubled['v'] / 10, 'feedback': call('shout', 'hi')}\n"
    )
    runtime = RecordingRuntime()
    stages = {name: STAGES[name] for name in ("double", "shout")}
    scorer, archive = _stage_scorer(tmp_path, code, runtime, stages)
    try:
        assert scorer.score("") == {"score": pytest.approx(0.6), "feedback": "HI"}
        version = _patch(tmp_path / "v1", archive, {"src/stages.py": STAGE_TREE["src/stages.py"].replace("2", "3")})
        assert scorer.score(version)["score"] == pytest.approx(0.9)
    finally:
        scorer.close()
    assert len(runtime.specs) == 4
    assert runtime.closed == 4
    keys = [spec.operation_key for spec in runtime.specs]
    assert len(set(keys)) == 4
    assert all(key.startswith("stage:") for key in keys)
    assert all(spec.network_disabled for spec in runtime.specs)


def test_the_guest_waits_as_long_as_a_stage_box_may_live(tmp_path: Path) -> None:
    """The guest's wait covers the stage layout and the scorer, so it never stops before the parent answers."""
    code = "from skynet import call\ndef score(repo_path, case=None):\n    return call('double', {'v': 1})['v'] / 10, 'ok'\n"
    runtime = RecordingRuntime()
    scorer, _ = _stage_scorer(tmp_path, code, runtime, {"double": STAGES["double"]})
    try:
        scorer.score("")
    finally:
        scorer.close()
    assert runtime.specs[0].lifetime_seconds == 60 + _STAGE_LAYOUT_SECONDS
    assert runtime.specs[0].lifetime_seconds + 1_800 <= 60 + SETUP_ALLOWANCE_SECONDS


def test_one_stage_is_enough(tmp_path: Path) -> None:
    """A run with one stage opens one box for each evaluation."""
    code = "from skynet import call\ndef score(repo_path, case=None):\n    return call('double', {'v': 1})['v'] / 10, 'ok'\n"
    runtime = RecordingRuntime()
    scorer, _ = _stage_scorer(tmp_path, code, runtime, {"double": STAGES["double"]})
    try:
        assert scorer.score("")["score"] == pytest.approx(0.2)
    finally:
        scorer.close()
    assert runtime.closed == 1


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("broken", "RuntimeError: stage broke"),
        ("slow", "passed its 1s timeout"),
        ("nope", "Unknown stage 'nope'"),
    ],
)
def test_stage_failures_reach_the_scorer_as_stage_errors(tmp_path: Path, name: str, expected: str) -> None:
    """A failed stage, a stage past its timeout and an unknown name each raise ``StageError``."""
    code = (
        "from skynet import call, StageError\n"
        "def score(repo_path, case=None):\n"
        "    try:\n"
        f"        call({name!r}, 3)\n"
        "    except StageError as error:\n"
        "        return 0.0, str(error)\n"
        "    return 1.0, 'no error'\n"
    )
    runtime = RecordingRuntime()
    scorer, _ = _stage_scorer(tmp_path, code, runtime)
    try:
        assert expected in scorer.score("")["feedback"]
    finally:
        scorer.close()
    assert runtime.closed == len(STAGES)


def test_a_box_that_fails_to_open_leaves_none_open(tmp_path: Path) -> None:
    """When one stage box cannot open, the open ones close and the version gets no score."""
    runtime = RecordingRuntime(fail_on="shout")
    scorer, _ = _stage_scorer(tmp_path, "def score(repo_path, case=None):\n    return 1.0, 'ran'\n", runtime)
    try:
        with pytest.raises(RepoScoreError, match="stage boxes could not open"):
            scorer.score("")
    finally:
        scorer.close()
    assert runtime.specs
    assert runtime.closed == len(runtime.specs)


def test_a_budget_refusal_stops_the_run(tmp_path: Path) -> None:
    """A stage box the budget refuses stops the run as budget reached, with no box left open."""
    runtime = RecordingRuntime(fail_on="shout", error=BudgetReached("spent"))
    scorer, _ = _stage_scorer(tmp_path, "def score(repo_path, case=None):\n    return 1.0, 'ran'\n", runtime)
    try:
        with pytest.raises(BudgetReached):
            scorer.score("")
    finally:
        scorer.close()
    assert runtime.closed == len(runtime.specs)


def test_a_partial_line_before_a_call_does_not_hide_the_frame(tmp_path: Path) -> None:
    """A scorer that prints without a newline before ``call`` still gets the stage's answer."""
    code = (
        "from skynet import call\n"
        "def score(repo_path, case=None):\n"
        "    print('x', end='', flush=True)\n"
        "    return call('double', {'v': 2})['v'] / 10, 'ok'\n"
    )
    runtime = RecordingRuntime()
    scorer, _ = _stage_scorer(tmp_path, code, runtime, {"double": STAGES["double"]})
    try:
        started = time.monotonic()
        assert scorer.score("")["score"] == pytest.approx(0.4)
        assert time.monotonic() - started < 30
    finally:
        scorer.close()


def test_calls_reach_their_stage_while_another_thread_prints(tmp_path: Path) -> None:
    """A thread that keeps printing partial lines never makes a call wait for the scorer timeout."""
    code = (
        "import threading\n"
        "from skynet import call\n"
        "def score(repo_path, case=None):\n"
        "    stop = threading.Event()\n"
        "    def chatter():\n"
        "        while not stop.is_set():\n"
        "            print('noise', end='', flush=True)\n"
        "    thread = threading.Thread(target=chatter)\n"
        "    thread.start()\n"
        "    try:\n"
        "        total = sum(call('double', {'v': value})['v'] for value in range(5))\n"
        "    finally:\n"
        "        stop.set()\n"
        "        thread.join()\n"
        "    return total / 100, 'ok'\n"
    )
    runtime = RecordingRuntime()
    scorer, _ = _stage_scorer(tmp_path, code, runtime, {"double": STAGES["double"]})
    try:
        started = time.monotonic()
        assert scorer.score("")["score"] == pytest.approx(0.2)
        assert time.monotonic() - started < 30
    finally:
        scorer.close()


def test_concurrent_calls_to_one_stage_run_in_parallel(tmp_path: Path) -> None:
    """Four calls to one stage overlap, each in its own process, up to ``max_concurrency``."""
    code = (
        "from concurrent.futures import ThreadPoolExecutor\n"
        "from skynet import call\n"
        "def score(repo_path, case=None):\n"
        "    with ThreadPoolExecutor(4) as pool:\n"
        "        values = list(pool.map(lambda value: call('slow', value), [1.5] * 4))\n"
        "    return sum(values), 'ok'\n"
    )
    stages = {"slow": TargetStage(entry="src/stages.py:slow", timeout=30)}
    runtime = RecordingRuntime()
    scorer, _ = _stage_scorer(tmp_path, code, runtime, stages, max_concurrency=4)
    try:
        started = time.monotonic()
        assert scorer.score("")["score"] == pytest.approx(6.0)
        # One call after another takes at least 6 s. The wide margin keeps a
        # loaded test host, where each process start is slow, from failing it.
        assert time.monotonic() - started < 4.5
    finally:
        scorer.close()


def test_stage_answers_keep_a_clean_version_scored_when_writes_touch_every_file(tmp_path: Path) -> None:
    """Each answer write moves every file's change time, yet a clean version keeps its score."""
    code = "from skynet import call\ndef score(repo_path, case=None):\n    return call('double', {'v': 3})['v'] / 10, 'ok'\n"
    scorer, _ = _stage_scorer(
        tmp_path,
        code,
        RecordingRuntime(),
        {"double": STAGES["double"]},
        scorer_runtime=_WriteTouchingRuntime(),
        protected_paths=("judge",),
    )
    try:
        assert scorer.score("")["score"] == pytest.approx(0.6)
    finally:
        scorer.close()


def test_a_protected_change_before_a_stage_call_still_blocks_the_version(tmp_path: Path) -> None:
    """A version that changes a protected file and then calls a stage gets no score, with the reason named once."""
    code = (
        "import os\n"
        "from skynet import call\n"
        "def score(repo_path, case=None):\n"
        "    with open(os.path.join(repo_path, 'judge/labels.csv'), 'w') as handle:\n"
        "        handle.write('9\\n')\n"
        "    total = sum(call('double', {'v': v})['v'] for v in (1, 2, 3))\n"
        "    return total / 100, 'ok'\n"
    )
    scorer, _ = _stage_scorer(
        tmp_path,
        code,
        RecordingRuntime(),
        {"double": STAGES["double"]},
        scorer_runtime=_WriteTouchingRuntime(),
        protected_paths=("judge",),
    )
    try:
        with pytest.raises(RepoScoreError, match=r"'judge/labels\.csv' is protected") as caught:
            scorer.score("")
    finally:
        scorer.close()
    assert str(caught.value).count("'judge/labels.csv' is protected") == 1


class _AnswerCheckFailingSession(_WriteTouchingSession):
    """Local box whose joined answer check fails, as when the box stops answering."""

    def run(self, command: str, **options: Any) -> Any:
        """Fail the answer check and run every other command.

        Args:
            command: Shell command line.
            **options: Run options.

        Returns:
            A failed result for the answer check, else the local result.
        """
        if command.startswith("export APP_ANSWER_PATH"):
            return CommandResult(exit_code=1)
        return self._session.run(command, **options)


class _AnswerCheckFailingRuntime(LocalSubprocessRuntime):
    """Local runtime whose boxes fail the answer check."""

    def open(self, spec: SandboxSpec) -> Any:
        """Open a local box wrapped so the answer check fails.

        Args:
            spec: The box's spec.

        Returns:
            The wrapped box.
        """
        return _AnswerCheckFailingSession(super().open(spec))


def test_a_failed_answer_check_gets_no_score_without_a_protected_path_block(tmp_path: Path) -> None:
    """A check that fails during a stage answer is a plain scoring failure, not a protected-file change."""
    code = "from skynet import call\ndef score(repo_path, case=None):\n    return call('double', {'v': 3})['v'] / 10, 'ok'\n"
    scorer, _ = _stage_scorer(
        tmp_path,
        code,
        RecordingRuntime(),
        {"double": STAGES["double"]},
        scorer_runtime=_AnswerCheckFailingRuntime(),
        protected_paths=("judge",),
    )
    try:
        started = time.monotonic()
        with pytest.raises(RepoScoreError, match="could not be checked or delivered") as caught:
            scorer.score("")
        assert time.monotonic() - started < 30
    finally:
        scorer.close()
    assert "is protected" not in str(caught.value)


class _LayoutFailingRuntime(RecordingRuntime):
    """Open stage boxes whose file writes fail, as when the archive upload breaks."""

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Open one local box whose writes raise.

        Args:
            spec: The stage box's spec.

        Returns:
            The local session, whose ``write_files`` raises ``ServiceError``.
        """
        session = super().open(spec)

        def refuse(files: Mapping[str, str]) -> None:
            """Fail every write.

            Args:
                files: The files the layout tried to write.

            Raises:
                ServiceError: Always.
            """
            raise ServiceError("upload failed")

        session.write_files = refuse  # type: ignore[method-assign]
        return session


def test_a_layout_failure_gets_no_score_and_closes_every_box(tmp_path: Path) -> None:
    """A stage box that cannot take the tree fails the version with a fixed message, not a server error."""
    runtime = _LayoutFailingRuntime()
    scorer, _ = _stage_scorer(tmp_path, "def score(repo_path, case=None):\n    return 1.0, 'ran'\n", runtime)
    try:
        with pytest.raises(RepoScoreError, match="could not lay out the version"):
            scorer.score("")
    finally:
        scorer.close()
    assert runtime.closed == len(runtime.specs)


class _Workspace:
    """Record each stage answer, or fail its check."""

    def __init__(self, *, checks: bool = True) -> None:
        """Start with no answers.

        Args:
            checks: Whether the protected files can be checked.
        """
        self.writes: list[tuple[str, str]] = []
        self._checks = checks

    def answer(self, path: str, text: str) -> tuple[str, ...] | None:
        """Record one answer.

        Args:
            path: Where the answer goes.
            text: The answer.

        Returns:
            No change, or ``None`` when the check fails.
        """
        if not self._checks:
            return None
        self.writes.append((path, text))
        return ()


def _relay(workspace: _Workspace) -> _StageRelay:
    """Build a relay with no stage boxes around a fake workspace.

    Args:
        workspace: Records the answers.

    Returns:
        The relay.
    """
    return _StageRelay(
        {},
        {},
        response_dir="calls/000001/stages",
        max_concurrency=1,
        env={},
        workspace=workspace,  # type: ignore[arg-type]
    )


def test_an_answer_after_the_relay_closes_never_reaches_the_scorer_box() -> None:
    """A stage call that ends after its evaluation writes nothing into the next one's box."""
    workspace = _Workspace()
    relay = _relay(workspace)
    relay._answer("a" * 32, {"output": 1})
    relay.close()
    relay._answer("b" * 32, {"output": 2})
    assert workspace.writes == [(f"calls/000001/stages/{'a' * 32}.json", '{"output": 1}')]


def test_an_answer_whose_check_fails_is_not_counted_as_a_protected_change() -> None:
    """A failed check marks the relay unchecked and adds no reason for the rule judge."""
    relay = _relay(_Workspace(checks=False))
    relay._answer("a" * 32, {"output": 1})
    relay.close()
    assert relay.unchecked
    assert relay.changes == []


def test_the_relay_finds_a_frame_that_does_not_start_its_line() -> None:
    """Text before the frame on one line is kept as log output, and the frame still gets its answer."""
    workspace = _Workspace()
    relay = _relay(workspace)
    identity = "c" * 32
    relay.output("stdout", "partial" + relay._prefix + json.dumps({"id": identity, "stage": "nope", "input": 1}) + "\n")
    deadline = time.monotonic() + 5
    while not workspace.writes and time.monotonic() < deadline:
        time.sleep(0.01)
    relay.close()
    assert [path for path, _ in workspace.writes] == [f"calls/000001/stages/{identity}.json"]
    assert "Unknown stage 'nope'" in workspace.writes[0][1]


class _Upstream:
    """Serve a stand-in for a user's own API on loopback and record what it receives."""

    def __init__(self) -> None:
        """Start the server on a free port."""
        self.requests: list[dict[str, Any]] = []
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            """Echo each request's path and body as JSON."""

            def do_POST(self) -> None:
                """Record the request and answer it."""
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                upstream.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                payload = json.dumps({"grade": 7, "path": self.path, "body": body.decode()}).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Upstream", "yes")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                """Keep the test output quiet."""

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        """Stop the server."""
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def upstream() -> Iterator[_Upstream]:
    """Run a stand-in service for one test.

    Yields:
        The running service.
    """
    server = _Upstream()
    yield server
    server.close()


SERVICE_SCORER = """
import json
import os
import urllib.request

def score(repo_path, case=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        os.environ["SKYNET_SERVICE_JUDGE_API_URL"] + "grade?mode=strict",
        data=b'{"text": "hi"}',
        headers={"Content-Type": "application/json", "Authorization": "Bearer guessed"},
        method="POST",
    )
    with opener.open(request) as response:
        answer = json.loads(response.read())
        upstream = response.headers.get("X-Upstream")
        status = response.status
    seen = {
        "status": status,
        "upstream": upstream,
        "path": answer["path"],
        "body": answer["body"],
        "key_in_env": any(value == "judge-secret-value" for value in os.environ.values()),
        "key_name_in_env": "JUDGE_KEY" in os.environ,
        "other_secret": os.environ.get("OTHER_KEY") == "other-value",
    }
    return answer["grade"] / 10, json.dumps(seen, sort_keys=True)
"""


def _service_scorer(
    tmp_path: Path,
    port: int,
    roles: Mapping[str, tuple[str, ...]],
    run_entry: str | None = None,
    code: str = SERVICE_SCORER,
) -> RepoScorer:
    """Open a scorer whose run declares one url service with a key.

    Args:
        tmp_path: Scratch folder.
        port: The stand-in service's port.
        roles: The services each role lists.
        run_entry: The ``[run]`` entry, if any.
        code: The metric's source.

    Returns:
        The scorer.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "src/cand.py": _SERVICE_CANDIDATE})
    return RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=120, network_disabled=True),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={"JUDGE_KEY": "judge-secret-value", "OTHER_KEY": "other-value"},
        ),
        code,
        timeout_seconds=60,
        services={"judge-api": ServiceSpec(secrets=("JUDGE_KEY",), url=f"http://judge.example.test:{port}/v1/")},
        service_roles=roles,
        run_entry=run_entry,
        secret_hosts={"JUDGE_KEY": "judge.example.test"},
        secret_allowed_hosts={"JUDGE_KEY": ("judge.example.test",)},
    )


def test_the_metric_reaches_its_listed_service_without_holding_the_key(
    tmp_path: Path, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent adds the key and calls the url; the scorer box gets the response but never the key."""
    resolved: list[str] = []

    def pinned(url: str, allow_private: bool, *, label: str) -> str:
        """Resolve the stand-in host to loopback, as public DNS would to a public address."""
        assert allow_private is False
        resolved.append(url)
        return "127.0.0.1"

    monkeypatch.setattr(repo_scorer, "_pinned_address", pinned)
    scorer = _service_scorer(tmp_path, upstream.port, {"run": (), "metric": ("judge-api",)})
    try:
        result = scorer.score("")
    finally:
        scorer.close()
    assert result["score"] == pytest.approx(0.7)
    assert json.loads(result["feedback"]) == {
        "body": '{"text": "hi"}',
        "key_in_env": False,
        "key_name_in_env": False,
        "other_secret": False,
        "path": "/v1/grade?mode=strict",
        "status": 201,
        "upstream": "yes",
    }
    assert resolved == [f"http://judge.example.test:{upstream.port}/v1/"]
    [request] = upstream.requests
    assert request["headers"]["Authorization"] == "Bearer judge-secret-value"
    assert request["headers"]["Host"] == f"judge.example.test:{upstream.port}"


_SERVICE_CANDIDATE = """
import json
import os
import urllib.request


def run(value):
    url = os.environ.get("SKYNET_SERVICE_JUDGE_API_URL")
    seen = {
        "key_in_env": any(v in ("judge-secret-value", "other-value") for v in os.environ.values()),
        "service_url": url is not None,
    }
    if url:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(url + "grade", data=b"{}", method="POST")
        with opener.open(request) as response:
            seen["grade"] = json.loads(response.read())["grade"]
    return seen
"""

_OUTPUT_SCORER = """
import json

def score(repo_path, case, output):
    return 1.0, json.dumps(output, sort_keys=True)
"""


def test_a_run_role_without_the_service_never_sees_its_key_or_url(
    tmp_path: Path, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the service listed only for the metric, the candidate gets no key and no relay url (E38)."""
    monkeypatch.setattr(repo_scorer, "_pinned_address", lambda url, allow_private, *, label: "127.0.0.1")
    scorer = _service_scorer(
        tmp_path,
        upstream.port,
        {"run": (), "metric": ("judge-api",)},
        run_entry="src/cand.py:run",
        code=_OUTPUT_SCORER,
    )
    try:
        result = scorer.score("", {"input": 1})
    finally:
        scorer.close()
    assert json.loads(result["feedback"]) == {"key_in_env": False, "service_url": False}
    assert upstream.requests == []


def test_a_run_role_reaches_its_listed_service_through_the_relay(
    tmp_path: Path, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service the ``[run]`` role lists answers the candidate through the relay; the key stays in the parent."""
    monkeypatch.setattr(repo_scorer, "_pinned_address", lambda url, allow_private, *, label: "127.0.0.1")
    scorer = _service_scorer(
        tmp_path,
        upstream.port,
        {"run": ("judge-api",), "metric": ()},
        run_entry="src/cand.py:run",
        code=_OUTPUT_SCORER,
    )
    try:
        result = scorer.score("", {"input": 1})
    finally:
        scorer.close()
    assert json.loads(result["feedback"]) == {"grade": 7, "key_in_env": False, "service_url": True}
    [request] = upstream.requests
    assert request["headers"]["Authorization"] == "Bearer judge-secret-value"


def _service_relay(
    workspace: _Workspace,
    *,
    role: str,
    allowed: tuple[str, ...],
    services: Mapping[str, ServiceSpec],
    host: str = "judge.example.test",
    allowed_hosts: tuple[str, ...] | None = None,
    secret: str = "judge-secret-value",
    recorder: CallRecorder | None = None,
) -> _StageRelay:
    """Build a relay with no stage boxes that forwards to the given services.

    Args:
        workspace: Records the answers.
        role: The calling role.
        allowed: The services that role lists.
        services: The run's services.
        host: The host ``JUDGE_KEY`` belongs to.
        allowed_hosts: The run's allowed hosts for ``JUDGE_KEY``; ``None`` allows ``host``.
        secret: The value of ``JUDGE_KEY``.
        recorder: Writes each relayed call into the run record.

    Returns:
        The relay.
    """
    return _StageRelay(
        {},
        {},
        response_dir="calls/000001/stages",
        max_concurrency=2,
        env={},
        workspace=workspace,  # type: ignore[arg-type]
        services=services,
        role=role,
        allowed=allowed,
        secrets={"JUDGE_KEY": secret},
        secret_hosts={"JUDGE_KEY": host},
        allowed_hosts={"JUDGE_KEY": (host,) if allowed_hosts is None else allowed_hosts},
        recorder=recorder,
    )


def _service_answer(
    relay: _StageRelay, workspace: _Workspace, name: str, path: str = "grade", method: str = "POST"
) -> dict[str, Any]:
    """Send one service frame through the relay and wait for its answer.

    Args:
        relay: The relay.
        workspace: Records the answers.
        name: The service the frame names.
        path: The request path under the service url.
        method: The request method.

    Returns:
        The answer the scorer box gets.
    """
    frame = {"id": "d" * 32, "service": name, "path": path, "method": method, "headers": [], "body": ""}
    relay.output("stdout", relay._prefix + json.dumps(frame) + "\n")
    deadline = time.monotonic() + 10
    while not workspace.writes and time.monotonic() < deadline:
        time.sleep(0.01)
    relay.close()
    [(_, text)] = workspace.writes
    return json.loads(text)


def test_a_role_that_does_not_list_the_service_is_refused(upstream: _Upstream) -> None:
    """The caller's role is checked in the relay; the refusal names the service and the role."""
    workspace = _Workspace()
    spec = ServiceSpec(secrets=("JUDGE_KEY",), url=f"http://judge.example.test:{upstream.port}/")
    relay = _service_relay(workspace, role="run", allowed=(), services={"judge": spec})
    assert relay.config("/work")["services"] == []
    answer = _service_answer(relay, workspace, "judge")
    assert answer["status"] == 403
    assert answer["error"] == ("Service 'judge' is not listed for the [run] role. Add it: [run] services = [\"judge\"]")
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("name", "status", "message"),
    [
        ("nope", 404, "Service 'nope' is not in optimize.toml. Add a [services.nope] table."),
        ("github", 403, "only services with a url go through the relay"),
    ],
)
def test_unknown_services_and_catalog_connectors_are_refused(name: str, status: int, message: str) -> None:
    """Only a declared service with a url goes out; a catalog connector stays with its own broker."""
    workspace = _Workspace()
    services = {"github": ServiceSpec(secrets=("GITHUB_TOKEN",))}
    relay = _service_relay(workspace, role="metric", allowed=("github",), services=services)
    assert relay.config("/work")["services"] == []
    answer = _service_answer(relay, workspace, name)
    assert answer["status"] == status
    assert message in answer["error"]


@pytest.mark.parametrize(
    ("name", "status", "message"),
    [
        ("judge", 403, "Service 'judge' is not listed for the [metric] role."),
        ("Judge", 404, "Service 'Judge' is not in optimize.toml. Add a [services.Judge] table."),
        ("JUDGE", 404, "Service 'JUDGE' is not in optimize.toml."),
        ("judge/x", 404, "Service 'judge/x' is not in optimize.toml."),
        ("../judge", 404, "Service '../judge' is not in optimize.toml."),
        ("", 404, "is not in optimize.toml."),
    ],
)
def test_a_name_that_only_looks_like_a_declared_service_is_unknown(
    upstream: _Upstream, name: str, status: int, message: str
) -> None:
    """Service names match exactly, as TOML tables do: another case or a slash is a 404, never the declared service."""
    workspace = _Workspace()
    spec = ServiceSpec(secrets=("JUDGE_KEY",), url=f"http://judge.example.test:{upstream.port}/")
    relay = _service_relay(workspace, role="metric", allowed=(), services={"judge": spec})
    answer = _service_answer(relay, workspace, name)
    assert answer["status"] == status
    assert message in answer["error"]
    assert upstream.requests == []


def test_a_service_whose_host_resolves_to_a_private_address_is_refused(upstream: _Upstream) -> None:
    """The relay resolves the host and refuses loopback, so a url cannot reach the parent's own network."""
    workspace = _Workspace()
    spec = ServiceSpec(secrets=("JUDGE_KEY",), url=f"http://127.0.0.1:{upstream.port}/")
    relay = _service_relay(workspace, role="metric", allowed=("judge",), services={"judge": spec}, host="127.0.0.1")
    assert relay.config("/work")["services"] == ["judge"]
    answer = _service_answer(relay, workspace, "judge")
    assert answer == {"status": 502, "error": "Service 'judge' could not be reached."}
    assert upstream.requests == []


class _Echo:
    """Serve an upstream that echoes the Authorization header, or redirects, and record each path."""

    def __init__(self) -> None:
        """Start the server on a free port."""
        self.paths: list[str] = []
        self.location = "/leak"
        echo = self

        class Handler(http.server.BaseHTTPRequestHandler):
            """Echo the key at ``/echo``, redirect at ``/redirect``, and answer anything else."""

            def do_GET(self) -> None:
                """Record the path and answer it."""
                echo.paths.append(self.path)
                if self.path.endswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", echo.location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                key = self.headers.get("Authorization", "")
                token = key.partition(" ")[2]
                if self.path.endswith("/split"):
                    # No Content-Length: the body ends when the connection closes, so the two halves arrive apart.
                    self.send_response(200)
                    self.end_headers()
                    half = len(token) // 2
                    for part in (b'{"seen": "' + token[:half].encode(), token[half:].encode() + b'"}'):
                        self.wfile.write(part)
                        self.wfile.flush()
                        time.sleep(0.05)
                    return
                if self.path.endswith("/base64"):
                    key = base64.b64encode(token.encode()).decode()
                elif self.path.endswith("/percent"):
                    key = urllib.parse.quote(token, safe="")
                payload = json.dumps({"seen": key}).encode()
                self.send_response(200)
                self.send_header("X-Echo", key)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:
                """Read the body, then answer as for a GET."""
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.do_GET()

            def log_message(self, format: str, *args: Any) -> None:
                """Keep the test output quiet."""

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        """Stop the server."""
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def echo(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Echo]:
    """Run an echoing upstream and resolve ``judge.example.test`` to it.

    Yields:
        The running upstream.
    """
    monkeypatch.setattr(repo_scorer, "_pinned_address", lambda url, allow_private, *, label: "127.0.0.1")
    server = _Echo()
    yield server
    server.close()


def _echo_relay(workspace: _Workspace, port: int, **hosts: Any) -> _StageRelay:
    """Build a metric relay to the echo upstream with ``JUDGE_KEY`` in its Authorization header.

    Args:
        workspace: Records the answers.
        port: The upstream's port.
        **hosts: ``host``, ``allowed_hosts`` and ``secret`` overrides for ``_service_relay``.

    Returns:
        The relay.
    """
    spec = ServiceSpec(
        secrets=("JUDGE_KEY",),
        url=f"http://judge.example.test:{port}/",
        headers={"Authorization": "Bearer ${JUDGE_KEY}"},
    )
    return _service_relay(workspace, role="metric", allowed=("judge",), services={"judge": spec}, **hosts)


def test_an_upstream_that_echoes_the_key_gives_the_box_only_the_redacted_form(echo: _Echo) -> None:
    """The upstream gets the key, and the box gets ``[REDACTED]`` in both the body and the headers (E56)."""
    workspace = _Workspace()
    answer = _service_answer(_echo_relay(workspace, echo.port), workspace, "judge", path="echo", method="GET")

    assert answer["status"] == 200
    body = base64.b64decode(answer["body"]).decode()
    assert json.loads(body) == {"seen": "[REDACTED]"}
    assert ["X-Echo", "[REDACTED]"] in answer["headers"]
    assert "judge-secret-value" not in json.dumps(answer)
    assert echo.paths == ["/echo"]


_REDIRECT_TARGETS = pytest.mark.parametrize(
    "location",
    ["/leak", "http://judge.example.test:{port}/leak", "http://elsewhere.example.test:{port}/leak"],
    ids=["same-host-path", "allowed-host-url", "other-host-url"],
)


@_REDIRECT_TARGETS
def test_a_redirect_comes_back_to_the_box_and_is_never_followed(echo: _Echo, location: str) -> None:
    """The relay returns the 302 as it is, to the allowed host or another one, so the key goes nowhere else (E53)."""
    echo.location = location.format(port=echo.port)
    sink = _CallSink()
    workspace = _Workspace()
    relay = _echo_relay(workspace, echo.port, recorder=CallRecorder(sink, "opt-1", "cand-1", ""))
    answer = _service_answer(relay, workspace, "judge", path="redirect", method="GET")

    assert answer["status"] == 302
    assert ["Location", echo.location] in answer["headers"]
    assert echo.paths == ["/redirect"]
    [(_, call, _)] = sink.calls
    assert (call["kind"], call["target"], call["status"]) == ("host", "judge.example.test", "ok")


def test_a_key_split_across_two_streamed_chunks_comes_back_redacted(echo: _Echo) -> None:
    """The relay reads the whole answer before it redacts, so a key cut across two chunks is still hidden (E56)."""
    workspace = _Workspace()
    answer = _service_answer(_echo_relay(workspace, echo.port), workspace, "judge", path="split", method="GET")

    assert answer["status"] == 200
    assert json.loads(base64.b64decode(answer["body"])) == {"seen": "[REDACTED]"}
    assert echo.paths == ["/split"]


@pytest.mark.parametrize(
    ("path", "encode"),
    [
        ("echo", lambda token: token),
        ("base64", lambda token: base64.b64encode(token.encode()).decode()),
        ("percent", lambda token: urllib.parse.quote(token, safe="")),
    ],
)
def test_a_re_encoded_key_echo_is_redacted(echo: _Echo, path: str, encode: Any) -> None:
    """A key echoed raw, base64-encoded or percent-encoded reaches neither the box nor the run record (E56, E28)."""
    secret = "judge/secret+value=1"
    sink = _CallSink()
    workspace = _Workspace()
    relay = _echo_relay(workspace, echo.port, secret=secret, recorder=CallRecorder(sink, "opt-1", "cand-1", ""))
    answer = _service_answer(relay, workspace, "judge", path=path, method="GET")

    assert answer["status"] == 200
    assert encode(secret) not in json.dumps(answer)
    assert json.loads(base64.b64decode(answer["body"])) == {"seen": "[REDACTED]"}
    [(_, call, secrets)] = sink.calls
    assert call["status"] == "ok"
    assert encode(secret) not in json.dumps(call, default=str)
    # The store masks each listed form in every field it writes; a base64 form is listed without its padding.
    assert encode(secret).rstrip("=") in secrets


@pytest.mark.parametrize(
    ("hosts", "fragment"),
    [
        ({"host": "other.example.test"}, "JUDGE_KEY"),
        ({"allowed_hosts": ()}, "JUDGE_KEY"),
        ({"allowed_hosts": ("elsewhere.example.test",)}, "JUDGE_KEY"),
    ],
)
def test_a_secret_outside_its_host_or_the_allowed_hosts_is_refused(
    echo: _Echo, hosts: dict[str, Any], fragment: str
) -> None:
    """The relay sends nothing when the secret belongs to another host or the run does not allow the host (E50, E52)."""
    workspace = _Workspace()
    answer = _service_answer(_echo_relay(workspace, echo.port, **hosts), workspace, "judge", path="echo", method="GET")

    assert answer["status"] == 403
    assert answer["error"].startswith("Service 'judge' was refused: ")
    assert fragment in answer["error"]
    assert "judge-secret-value" not in answer["error"]
    assert echo.paths == []


def test_a_metric_that_does_not_list_the_service_gets_the_fix_in_the_error(echo: _Echo) -> None:
    """The metric's refusal names the exact line to add, and the run records the refused call (E55, E28)."""
    sink = _CallSink()
    workspace = _Workspace()
    spec = ServiceSpec(secrets=("JUDGE_KEY",), url=f"http://judge.example.test:{echo.port}/")
    relay = _service_relay(
        workspace,
        role="metric",
        allowed=(),
        services={"judge-api": spec},
        recorder=CallRecorder(sink, "opt-1", "cand-1", ""),
    )

    answer = _service_answer(relay, workspace, "judge-api")

    assert answer["status"] == 403
    assert answer["error"].endswith('Add it: [metric] services = ["judge-api"]')
    assert echo.paths == []
    [(_, call, _)] = sink.calls
    assert (call["candidate_id"], call["role"], call["kind"]) == ("cand-1", "metric", "host")
    assert (call["target"], call["status"], call["failure_reason"]) == ("judge-api", "refused", answer["error"])


def _mcp_relay(
    workspace: _Workspace,
    role: str,
    mcp: RoleEnvironment,
    *,
    services: Mapping[str, ServiceSpec] | None = None,
    secret: str = "judge-secret-value",
    recorder: CallRecorder | None = None,
) -> _StageRelay:
    """Build a relay with no stage boxes that forwards to the role's MCP servers.

    Args:
        workspace: Records the answers.
        role: The calling role.
        mcp: The role with the servers of its own MCP file.
        services: The run's services, which the role also lists.
        secret: The value of ``JUDGE_KEY``.
        recorder: Writes each relayed call into the run record.

    Returns:
        The relay.
    """
    return _StageRelay(
        {},
        {},
        response_dir="calls/000001/stages",
        max_concurrency=2,
        env={},
        workspace=workspace,  # type: ignore[arg-type]
        services=services,
        role=role,
        allowed=tuple(services or ()),
        mcp=mcp,
        secrets={"JUDGE_KEY": secret},
        secret_hosts={"JUDGE_KEY": "judge.example.test"},
        allowed_hosts={"JUDGE_KEY": ("judge.example.test",)},
        recorder=recorder,
    )


def _mcp_answer(
    relay: _StageRelay, workspace: _Workspace, name: str, path: str = "", body: bytes = b""
) -> dict[str, Any]:
    """Send one MCP frame through the relay and wait for its answer.

    Args:
        relay: The relay.
        workspace: Records the answers.
        name: The server the frame names.
        path: The request path under the server's url.
        body: A JSON-RPC body to POST; empty sends a GET with no body.

    Returns:
        The answer the box gets.
    """
    frame = {
        "id": "e" * 32,
        "mcp": name,
        "path": path,
        "method": "POST" if body else "GET",
        "headers": [],
        "body": base64.b64encode(body).decode(),
    }
    relay.output("stdout", relay._prefix + json.dumps(frame) + "\n")
    deadline = time.monotonic() + 10
    while not workspace.writes and time.monotonic() < deadline:
        time.sleep(0.01)
    relay.close()
    [(_, text)] = workspace.writes
    return json.loads(text)


def _mcp_role(port: int, endpoint: str = "echo") -> RoleEnvironment:
    """Return a role whose MCP file holds one ``search`` server on the echo upstream.

    Args:
        port: The upstream's port.
        endpoint: The server url's path.

    Returns:
        The role.
    """
    server = McpServer(
        name="search",
        url=f"http://judge.example.test:{port}/{endpoint}",
        headers={"Authorization": "Bearer ${JUDGE_KEY}"},
    )
    return RoleEnvironment(mcp=".mcp.json", mcp_servers=(server,))


def test_an_mcp_server_gets_the_key_and_the_box_gets_only_the_redacted_form(echo: _Echo) -> None:
    """An MCP call goes to the exact declared url with the key filled, and the echo comes back redacted (E49, E56)."""
    workspace = _Workspace()
    relay = _mcp_relay(workspace, "run", _mcp_role(echo.port))
    assert relay.config("/work")["mcp"] == ["search"]
    assert relay.config("/work")["mcp_file"] == ".mcp.json"

    answer = _mcp_answer(relay, workspace, "search")

    assert answer["status"] == 200
    assert json.loads(base64.b64decode(answer["body"])) == {"seen": "[REDACTED]"}
    assert ["X-Echo", "[REDACTED]"] in answer["headers"]
    assert "judge-secret-value" not in json.dumps(answer)
    assert echo.paths == ["/echo"]


class _CallSink:
    """Keep each run call the relay records, with the secret forms the store must remove."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[tuple[str, dict[str, Any], list[str]]] = []
        self.threads: list[str] = []

    def record_call(self, optimization_id: str, call: dict[str, Any], secrets: list[str]) -> int:
        """Keep one call.

        Args:
            optimization_id: The run.
            call: The call fields.
            secrets: The secret forms the store removes.

        Returns:
            The call's sequence number.
        """
        self.calls.append((optimization_id, call, secrets))
        self.threads.append(threading.current_thread().name)
        return len(self.calls)


@pytest.mark.parametrize("kind", ["host", "mcp"])
def test_a_relayed_call_is_recorded_under_its_role_and_kind_with_the_key_marked_secret(echo: _Echo, kind: str) -> None:
    """Each service and MCP call writes one run call for its role, and the store gets the key to remove (E28)."""
    sink = _CallSink()
    recorder = CallRecorder(sink, "opt-1", "cand-1", "")
    workspace = _Workspace()
    if kind == "mcp":
        relay = _mcp_relay(workspace, "run", _mcp_role(echo.port), recorder=recorder)
        answer = _mcp_answer(relay, workspace, "search")
    else:
        spec = ServiceSpec(
            secrets=("JUDGE_KEY",),
            url=f"http://judge.example.test:{echo.port}/",
            headers={"Authorization": "Bearer ${JUDGE_KEY}"},
        )
        relay = _service_relay(
            workspace, role="metric", allowed=("judge",), services={"judge": spec}, recorder=recorder
        )
        answer = _service_answer(relay, workspace, "judge", path="echo", method="GET")

    assert answer["status"] == 200
    [(optimization_id, call, secrets)] = sink.calls
    assert optimization_id == "opt-1"
    assert (call["candidate_id"], call["role"], call["kind"]) == ("cand-1", "run" if kind == "mcp" else "metric", kind)
    target = f"search@judge.example.test:{echo.port}/" if kind == "mcp" else "judge.example.test"
    assert (call["target"], call["status"]) == (target, "ok")
    assert "judge-secret-value" in secrets


def test_two_mcp_servers_on_one_host_each_get_their_own_record_target(echo: _Echo) -> None:
    """Each record names its server and tool, so two servers on one host never share a target (E28, E49)."""
    sink = _CallSink()
    recorder = CallRecorder(sink, "opt-1", "cand-1", "")
    servers = tuple(
        McpServer(
            name=name,
            url=f"http://judge.example.test:{echo.port}/{name}",
            headers={"Authorization": "Bearer ${JUDGE_KEY}"},
        )
        for name in ("search", "docs")
    )
    role = RoleEnvironment(mcp=".mcp.json", mcp_servers=servers)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "lookup"}}).encode()
    for name in ("search", "docs"):
        workspace = _Workspace()
        answer = _mcp_answer(_mcp_relay(workspace, "run", role, recorder=recorder), workspace, name, body=body)
        assert answer["status"] == 200

    assert [call["target"] for _, call, _ in sink.calls] == [
        f"search@judge.example.test:{echo.port}/lookup",
        f"docs@judge.example.test:{echo.port}/lookup",
    ]
    assert echo.paths == ["/search", "/docs"]


@pytest.mark.parametrize(("scope", "expected"), [({"candidate": "cand-x"}, "cand-x"), ({}, "run")])
def test_a_relayed_call_on_a_pool_thread_keeps_the_scorer_threads_candidate(
    echo: _Echo, scope: dict[str, str], expected: str
) -> None:
    """The candidate comes from the thread that built the relay, not from the pool thread; none gives the run bucket."""
    sink = _CallSink()
    workspace = _Workspace()
    with usage_scope(**scope):
        relay = _mcp_relay(workspace, "metric", _mcp_role(echo.port), recorder=CallRecorder(sink, "opt-1", "", ""))
    with usage_scope(candidate="other-candidate"):
        answer = _mcp_answer(relay, workspace, "search")

    assert answer["status"] == 200
    [(_, call, _)] = sink.calls
    assert sink.threads[0].startswith("stage-service")
    assert call["candidate_id"] == expected


class _BrokenSink:
    """Fail every write the way a lost database connection does, with the call in the error text."""

    def record_call(self, optimization_id: str, call: dict[str, Any], secrets: list[str]) -> int:
        """Raise an error whose text holds the call and its secret forms.

        Args:
            optimization_id: The run.
            call: The call fields.
            secrets: The secret forms the store removes.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError(f"database is gone: {call!r} {secrets!r}")


def test_a_recorder_failure_keeps_the_relayed_call_and_logs_no_secret(
    echo: _Echo, caplog: pytest.LogCaptureFixture
) -> None:
    """A store error during a relayed MCP call still returns the answer, and no log line holds the key or a header."""
    workspace = _Workspace()
    relay = _mcp_relay(workspace, "run", _mcp_role(echo.port), recorder=CallRecorder(_BrokenSink(), "opt-1", "", ""))
    # INFO is the lowest level the services log at; httpcore's DEBUG traces print raw upstream headers.
    with caplog.at_level(logging.INFO):
        answer = _mcp_answer(relay, workspace, "search")

    assert answer["status"] == 200
    assert "judge-secret-value" not in json.dumps(answer)
    assert any("Could not record a mcp call" in record.getMessage() for record in caplog.records)
    logged = "\n".join(record.getMessage() + str(record.exc_info or "") for record in caplog.records)
    assert "judge-secret-value" not in logged
    assert "Authorization" not in logged
    assert "database is gone" not in logged


@_REDIRECT_TARGETS
def test_an_mcp_redirect_comes_back_to_the_box_and_is_never_followed(echo: _Echo, location: str) -> None:
    """The relay returns an MCP server's 302 as it is, to the allowed host or another one (E53)."""
    echo.location = location.format(port=echo.port)
    sink = _CallSink()
    workspace = _Workspace()
    recorder = CallRecorder(sink, "opt-1", "cand-1", "")
    relay = _mcp_relay(workspace, "metric", _mcp_role(echo.port, "redirect"), recorder=recorder)
    answer = _mcp_answer(relay, workspace, "search")

    assert answer["status"] == 302
    assert ["Location", echo.location] in answer["headers"]
    assert echo.paths == ["/redirect"]
    [(_, call, _)] = sink.calls
    assert (call["role"], call["kind"], call["status"]) == ("metric", "mcp", "ok")
    assert call["target"] == f"search@judge.example.test:{echo.port}/"


@pytest.mark.parametrize(
    ("mcp", "message"),
    [
        (RoleEnvironment(), 'Add it to a .mcp.json file and set [metric] mcp = ".mcp.json".'),
        (
            RoleEnvironment(mcp="eval/.mcp.json"),
            "MCP server 'search' is not in eval/.mcp.json, the [metric] MCP file. Add it there.",
        ),
    ],
)
def test_a_role_reaches_only_the_servers_of_its_own_mcp_file(echo: _Echo, mcp: RoleEnvironment, message: str) -> None:
    """[metric] cannot call a server that only the [run] file holds, and the refusal names the fix (E55)."""
    workspace = _Workspace()
    relay = _mcp_relay(workspace, "metric", mcp)
    assert relay.config("/work")["mcp"] == []

    answer = _mcp_answer(relay, workspace, "search")

    assert answer["status"] == 403
    assert answer["error"].endswith(message)
    assert echo.paths == []


def test_an_mcp_name_and_a_service_name_never_cross(echo: _Echo) -> None:
    """``/mcp/judge`` cannot reach the ``judge`` service, and ``/service/search`` cannot reach the MCP server (E55)."""
    judge = ServiceSpec(
        secrets=("JUDGE_KEY",),
        url=f"http://judge.example.test:{echo.port}/",
        headers={"Authorization": "Bearer ${JUDGE_KEY}"},
    )
    workspace = _Workspace()
    mcp = _mcp_answer(_mcp_relay(workspace, "run", _mcp_role(echo.port), services={"judge": judge}), workspace, "judge")
    workspace = _Workspace()
    service = _service_answer(
        _mcp_relay(workspace, "run", _mcp_role(echo.port), services={"judge": judge}), workspace, "search", path="echo"
    )

    assert mcp["status"] == 403
    assert mcp["error"].endswith("MCP server 'judge' is not in .mcp.json, the [run] MCP file. Add it there.")
    assert service == {
        "status": 404,
        "error": "Service 'search' is not in optimize.toml. Add a [services.search] table.",
    }
    assert echo.paths == []


@pytest.mark.parametrize("kind", ["mcp", "service"])
def test_a_secret_the_http_library_rejects_never_reaches_the_log(
    echo: _Echo, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A filled header that h11 refuses gives a plain 502, and the log holds the error type, not the value."""
    workspace = _Workspace()
    secret = "judge-secret-value\nX-Injected: 1"
    with caplog.at_level(logging.WARNING, logger=repo_scorer.logger.name):
        if kind == "mcp":
            answer = _mcp_answer(_mcp_relay(workspace, "run", _mcp_role(echo.port), secret=secret), workspace, "search")
        else:
            relay = _echo_relay(workspace, echo.port, secret=secret)
            answer = _service_answer(relay, workspace, "judge", path="echo", method="GET")

    assert answer["status"] == 502
    assert "judge-secret-value" not in json.dumps(answer)
    assert "LocalProtocolError" in caplog.text
    assert "judge-secret-value" not in caplog.text
    assert echo.paths == []


@pytest.mark.parametrize("kind", ["mcp", "service"])
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (BudgetReached("spent"), "budget_reached"),
        (BudgetInsufficientError("short"), "budget_insufficient"),
        (UnpricedOperationError("no price"), "unpriced_operation"),
        (BudgetError("no cover"), "usage_pending"),
        (UsagePendingError("pending"), "usage_pending"),
    ],
)
def test_a_budget_stop_in_a_relayed_call_answers_the_box_at_once_and_stops_the_call(
    echo: _Echo, monkeypatch: pytest.MonkeyPatch, kind: str, error: BaseException, code: str
) -> None:
    """A budget stop gets the stage call's control code within 1 s, never a 403 or a lost answer, and the parent keeps it."""

    def stop(**_: Any) -> bytes:
        """Raise the budget stop as the metered relay does.

        Raises:
            BaseException: The parametrized stop.
        """
        raise error

    monkeypatch.setattr(repo_scorer, "relay_request", stop)
    workspace = _Workspace()
    relay = _mcp_relay(workspace, "run", _mcp_role(echo.port)) if kind == "mcp" else _echo_relay(workspace, echo.port)
    started = time.monotonic()
    if kind == "mcp":
        answer = _mcp_answer(relay, workspace, "search")
    else:
        answer = _service_answer(relay, workspace, "judge", path="echo", method="GET")

    assert time.monotonic() - started < 1
    assert answer["status"] == 503
    assert answer["control"] == {"code": code, "message": str(error)}
    assert relay.control == {"code": code, "message": str(error)}
    with pytest.raises(UsagePendingError if code == "usage_pending" else type(error)):
        repo_scorer._raise_control({"control": relay.control})


def test_the_scorer_gives_each_role_only_the_servers_of_its_own_mcp_file(tmp_path: Path) -> None:
    """The [run] relay holds only the [run] file's servers, and the [metric] relay only the [metric] file's (E55)."""
    run = RoleEnvironment(
        mcp=".mcp.json", mcp_servers=(McpServer(name="search", url="https://search.example.test/mcp", headers={}),)
    )
    metric = RoleEnvironment(
        mcp="eval/.mcp.json",
        mcp_servers=(McpServer(name="grader", url="https://grader.example.test/mcp", headers={}),),
    )
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=120, network_disabled=True),
            archive=_archive(tmp_path, {"src/app.py": "x = 1\n"}),
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
        ),
        SERVICE_SCORER,
        timeout_seconds=60,
        mcp_roles={"run": run, "metric": metric},
    )
    seen = {}
    for role, other in (("run", "grader"), ("metric", "search")):
        with scorer._service_relay(role, "calls/000001/stages") as relay:
            assert relay is not None
            seen[role] = (relay.config("/work")["mcp"], relay._mcp_refusal(other))

    assert seen["run"][0] == ["search"]
    assert seen["metric"][0] == ["grader"]
    assert seen["run"][1]["status"] == seen["metric"][1]["status"] == 403
    assert seen["run"][1]["error"].endswith(
        "MCP server 'grader' is not in .mcp.json, the [run] MCP file. Add it there."
    )
    assert seen["metric"][1]["error"].endswith(
        "MCP server 'search' is not in eval/.mcp.json, the [metric] MCP file. Add it there."
    )


_PROBE_CANDIDATE = """
import json
import os
import site
import subprocess
import sys

# The markers are split so this file does not match its own search.
METRIC_MARK = "WEIGHT" + " = 0.3"
CASE_MARK = "ANSWER" + "-42"


def _git(*args):
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return done.stdout + done.stderr


def probe(value):
    found = []
    if os.path.exists("eval/metric.py"):
        found.append("eval/metric.py in the tree")
    for text in (_git("show", "HEAD:eval/metric.py"), _git("log", "--all", "-p")):
        if METRIC_MARK in text:
            found.append("metric in git")
    root = os.path.abspath(os.path.join(os.getcwd(), "..", ".."))
    for folder, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            if name == "hidden.b64":
                found.append("hidden archive: " + os.path.relpath(path, root))
                continue
            try:
                with open(path, errors="ignore") as handle:
                    text = handle.read()
            except OSError:
                continue
            if METRIC_MARK in text or CASE_MARK in text:
                found.append("marker in " + os.path.relpath(path, root))
    if any(v == "run-secret-value" for v in os.environ.values()) or "RUN_KEY" in os.environ:
        found.append("run secret in env")
    subprocess.Popen(
        [sys.executable, "-c",
         "import time; time.sleep(1.5); open(%r, 'w').write(open('eval/metric.py').read())" % value["late"]],
        start_new_session=True,
    )
    return {"found": found, "input": value}


def plant(value):
    subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(1); open('../prepared/late.py', 'w').write('x')"],
        start_new_session=True,
    )
    return value


def boom(value):
    raise RuntimeError("candidate broke")


def echo(value):
    return value


FORGED_RUNNER = (
    "import json, os, sys\\n"
    "open(os.path.join(sys.argv[-1], 'output.json'), 'w').write(json.dumps({'score': 0.99, 'feedback': 'FORGED'}))\\n"
)


def tamper(value):
    # The checkout is .app-repo/work; the runner and the calls folder sit next to it.
    if value == "swap":
        os.rename("../calls", "../calls2")
        os.symlink("calls2", "../calls")
        return value
    with open("../app_runner.py", "w") as handle:
        handle.write(FORGED_RUNNER)
    for name in os.listdir("../calls"):
        with open(os.path.join("../calls", name, "output.json"), "w") as handle:
            handle.write(json.dumps({"score": 0.99, "feedback": "FORGED"}))
    return value


FORGE = (
    "import atexit, json, os, sys\\n"
    "def _forge():\\n"
    "    try:\\n"
    "        hidden = open('eval/metric.py').read()\\n"
    "    except OSError:\\n"
    "        hidden = 'unread'\\n"
    "    with open(os.path.join(sys.argv[-1], 'output.json'), 'w') as handle:\\n"
    "        handle.write(json.dumps({'score': 0.99, 'side_info': {'feedback': 'FORGED ' + hidden}, 'stopped': True}))\\n"
    "atexit.register(_forge)\\n"
)

# The planted json.py drops its own folder from the path and loads the real json before it forges.
JSON_PLANT = (
    "import os, sys\\n"
    "_here = os.path.dirname(os.path.abspath(__file__))\\n"
    "sys.path[:] = [p for p in sys.path if os.path.abspath(p or '.') != _here]\\n"
    "del sys.modules['json']\\n"
    "import json as _real\\n"
    "sys.modules['json'] = _real\\n"
) + FORGE


def plant_module(value):
    with open("../json.py", "w") as handle:
        handle.write(JSON_PLANT)
    return value


def plant_pth(value):
    folder = site.getusersitepackages()
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "skynet_forge.py"), "w") as handle:
        handle.write(FORGE)
    with open(os.path.join(folder, "skynet_forge.pth"), "w") as handle:
        handle.write("import skynet_forge\\n")
    return folder
"""

_PROBE_METRIC = """
import json
import os


def score(repo_path, case, output):
    weight = open(os.path.join(repo_path, "eval/metric.py")).read().strip()
    return 1.0, json.dumps({"output": output, "case": case, "weight": weight}, sort_keys=True)
"""


def _probed(
    tmp_path: Path, run_entry: str | None, code: str = _PROBE_METRIC, env: dict[str, str] | None = None
) -> RepoScorer:
    """Open a scorer with a hidden metric, a run secret and a ``[run]`` entry.

    Args:
        tmp_path: Scratch folder.
        run_entry: The ``[run]`` entry, if any.
        code: The metric's source.
        env: Extra box environment.

    Returns:
        The scorer.
    """
    archive = _archive(
        tmp_path,
        {"src/app.py": "x = 1\n", "src/cand.py": _PROBE_CANDIDATE, "eval/metric.py": "WEIGHT = 0.3\n"},
    )
    return RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=60, network_disabled=True, env=env or {}),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={"RUN_KEY": "run-secret-value"},
            hidden_paths=["eval"],
        ),
        code,
        timeout_seconds=60,
        run_entry=run_entry,
        run_timeout=10.0,
    )


def test_the_candidate_runs_without_the_metric_files_and_the_metric_sees_its_output(tmp_path: Path) -> None:
    """The candidate finds no hidden file, case answer or run secret, also on a second call (E38)."""
    late = tmp_path / "late.txt"
    scorer = _probed(tmp_path, "src/cand.py:probe")
    try:
        for _ in range(2):
            case = {"input": {"late": str(late)}, "expected": "ANSWER-42"}
            result = scorer.score("", case)
            seen = json.loads(result["feedback"])
            assert seen["output"] == {"found": [], "input": {"late": str(late)}}
            assert seen["case"] == case
            assert seen["weight"] == "WEIGHT = 0.3"
    finally:
        scorer.close()
    # A process the candidate left behind would copy the metric here once it is back.
    time.sleep(2)
    assert not late.exists()


def test_no_process_of_a_version_outlives_its_call_to_write_the_prepared_tree(tmp_path: Path) -> None:
    """Stop a version's detached process before the next checkout, so the box and its prepared tree stay clean (E38)."""
    scorer = _probed(tmp_path, "src/cand.py:plant")
    try:
        scorer.score("", {"input": 1})
        box = scorer._workspace.session()
        time.sleep(2)
        assert box.run(f"test ! -e {PREPARED_DIR}/late.py").ok
        scorer.score("", {"input": 2})
        assert scorer._workspace.session() is box
    finally:
        scorer.close()


def test_without_a_run_entry_no_process_of_the_metric_call_outlives_it(tmp_path: Path) -> None:
    """Stop what the metric call started when no ``[run]`` supervisor runs the version (E38)."""
    metric = (
        "import subprocess, sys\n"
        "def score(repo_path, case, output):\n"
        "    subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(1);"
        " open('../prepared/late.py', 'w').write('x')\"], start_new_session=True)\n"
        "    return 1.0, 'ok'\n"
    )
    scorer = _probed(tmp_path, None, code=metric)
    try:
        assert scorer.score("", {"input": 1})["score"] == 1.0
        box = scorer._workspace.session()
        time.sleep(2)
        assert box.run(f"test ! -e {PREPARED_DIR}/late.py").ok
        scorer.score("", {"input": 2})
        assert scorer._workspace.session() is box
    finally:
        scorer.close()


def test_the_metric_runs_the_real_runner_whatever_the_candidate_left(tmp_path: Path) -> None:
    """A candidate cannot rewrite the runner or plant the metric's output for it (E38)."""
    metric = "def score(repo_path, case, output):\n    return 0.25, 'real metric'\n"
    scorer = _probed(tmp_path, "src/cand.py:tamper", code=metric)
    try:
        for _ in range(2):
            result = scorer.score("", {"input": "forge"})
            assert result["score"] == 0.25
            assert result["feedback"] == "real metric"
    finally:
        scorer.close()


def _base_python(tmp_path: Path) -> dict[str, str]:
    """Put the interpreter without the test venv first on the box PATH, so user site-packages are on.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The box environment with that PATH.
    """
    base = getattr(sys, "_base_executable", sys.executable)
    probe = subprocess.run(
        [base, "-c", "import site; print(site.ENABLE_USER_SITE)"], capture_output=True, text=True, check=False
    )
    if probe.stdout.strip() != "True":
        pytest.skip("no interpreter with user site-packages on this host")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").symlink_to(base)
    return {"PATH": os.pathsep.join([str(shim), "/usr/bin", "/bin"])}


@pytest.mark.parametrize("entry", ["plant_module", "plant_pth"])
def test_a_module_the_candidate_plants_for_the_metric_runner_never_runs(tmp_path: Path, entry: str) -> None:
    """A json.py next to the runner or a user-site .pth cannot forge the score or read the metric (E38, E40)."""
    metric = "def score(repo_path, case, output):\n    return 0.25, 'real metric'\n"
    # The test venv turns user site-packages off; a box python3 has them on.
    env = _base_python(tmp_path) if entry == "plant_pth" else None
    scorer = _probed(tmp_path, f"src/cand.py:{entry}", code=metric, env=env)
    try:
        for _ in range(2):
            result = scorer.score("", {"input": "forge"})
            assert result["score"] == 0.25
            assert result["feedback"] == "real metric"
    finally:
        scorer.close()


_GIT_PLANT = r"""
import os


def go(value):
    home = os.environ["HOME"]
    marker = os.path.join(home, "filter_ran")
    if value != "plant":
        return {"filter_ran": os.path.exists(marker)}
    script = os.path.join(home, "f.sh")
    with open(script, "w") as handle:
        handle.write("#!/bin/sh\ntouch " + marker + "\ncat\n")
    os.chmod(script, 0o755)
    with open(os.path.join(home, "attrs"), "w") as handle:
        handle.write("* filter=x\n")
    for folder in (home, os.path.join(home, ".config", "git")):
        os.makedirs(folder, exist_ok=True)
    with open(os.path.join(home, ".config", "git", "attributes"), "w") as handle:
        handle.write("* filter=x\n")
    config = "[core]\n\tattributesFile = " + os.path.join(home, "attrs") + "\n[filter \"x\"]\n\tclean = " + script + "\n"
    for path in (os.path.join(home, ".gitconfig"), os.path.join(home, ".config", "git", "config")):
        with open(path, "w") as handle:
            handle.write(config)
    return {"filter_ran": False}
"""


def test_a_git_config_the_candidate_plants_in_home_never_runs_in_a_later_checkout(tmp_path: Path) -> None:
    """A filter set in $HOME or the XDG git config does not run in the next checkout's ``git apply`` (E38)."""
    files = {"src/app.py": "x = 1\n", "src/cand.py": _GIT_PLANT, "eval/metric.py": "WEIGHT = 0.3\n"}
    archive = _archive(tmp_path, files)
    patch = _patch(tmp_path / "version", archive, {"src/app.py": "x = 2\n"})
    workspace = RepoWorkspace(
        runtime=LocalSubprocessRuntime(),
        spec=SandboxSpec(lifetime_seconds=90, network_disabled=True),
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=None,
        secrets={},
        hidden_paths=["eval"],
    )
    metric = "def score(repo_path, case, output):\n    return 0.25, repr(output)\n"
    scorer = RepoScorer(workspace, metric, timeout_seconds=90, run_entry="src/cand.py:go", run_timeout=10.0)
    try:
        scorer.score("", {"input": "plant"})
        results = [scorer.score(version, {"input": "check"}) for version in (patch, "")]
    finally:
        scorer.close()
    assert [result["feedback"] for result in results] == ["{'filter_ran': False}"] * 2


def test_a_candidate_that_links_the_calls_folder_has_no_score_and_the_next_call_works(tmp_path: Path) -> None:
    """End the box when ``calls`` became a link; the next call gets a fresh box with its runner (E38)."""
    metric = "def score(repo_path, case, output):\n    return 0.25, 'real metric'\n"
    scorer = _probed(tmp_path, "src/cand.py:tamper", code=metric)
    try:
        with pytest.raises(RepoScoreError, match="replaced a folder of the scoring box with a link"):
            scorer.score("", {"input": "swap"})
        result = scorer.score("", {"input": "forge"})
    finally:
        scorer.close()
    assert result["score"] == 0.25
    assert result["feedback"] == "real metric"


def test_a_failed_run_scores_zero_and_skips_the_metric(tmp_path: Path) -> None:
    """A candidate that raises gets 0 and "Run failed:" feedback; the metric never runs (E38)."""
    marker = tmp_path / "metric-ran"
    metric = f"def score(repo_path, case, output):\n    open({str(marker)!r}, 'w').write('x')\n    return 1.0\n"
    scorer = _probed(tmp_path, "src/cand.py:boom", code=metric)
    try:
        result = scorer.score("", {"input": 1})
    finally:
        scorer.close()
    assert result["score"] == 0.0
    assert result["feedback"].startswith("Run failed: ")
    assert "candidate broke" in result["feedback"]
    assert len(result["feedback"]) <= 2_000
    assert not marker.exists()


def test_run_failed_feedback_keeps_the_version_frames_with_repo_paths(tmp_path: Path) -> None:
    """The proposer sees the version's own frames, but no box path and no metric file (E41, D43, E61)."""
    scorer = _probed(tmp_path, "src/cand.py:boom")
    try:
        scorer.prepare()
        reply = json.loads(scorer.dispatch({"candidate": "", "case": {"input": 1}}).body)
        root = scorer._root
    finally:
        scorer.close()
    feedback = reply["feedback"]
    assert reply["score"] == 0.0
    assert feedback.startswith("Run failed: Traceback (most recent call last):")
    assert 'File "src/cand.py"' in feedback
    assert 'raise RuntimeError("candidate broke")' in feedback
    assert feedback.endswith("RuntimeError: candidate broke")
    assert root
    assert root not in feedback
    assert str(tmp_path) not in feedback
    assert "app_runner" not in feedback
    assert "eval/" not in feedback
    assert len(feedback) <= 2_000


def test_run_failed_scrub_drops_metric_runner_and_library_frames() -> None:
    """Only frames in visible version files stay; metric, runner and library frames go (D43, E61)."""
    root = "/box/root"
    work = f"{root}/.app-repo/work"
    text = (
        "Run failed: Traceback (most recent call last):\n"
        f'  File "{root}/.app-repo/app_runner.py", line 1447, in run_stage\n'
        "    secret_runner_line()\n"
        f'  File "{work}/src/cand.py", line 3, in boom\n'
        "    helper()\n"
        f'  File "{work}/eval/metric.py", line 2, in helper\n'
        "    WEIGHT = 0.3\n"
        '  File "/usr/lib/python3.12/json/__init__.py", line 9, in dumps\n'
        "    return x\n"
        "RuntimeError: broke\n"
    )
    scrubbed = _scrub(text, root, ("eval",))
    assert scrubbed == (
        "Run failed: Traceback (most recent call last):\n"
        '  File "src/cand.py", line 3, in boom\n'
        "    helper()\n"
        "RuntimeError: broke"
    )
    assert _scrub(scrubbed, root, ("eval",)) == scrubbed
    assert _scrub(text.removeprefix("Run failed: "), root, ("eval",)) == "RuntimeError: broke"


def test_a_command_entry_with_a_nonzero_exit_still_reaches_the_metric(tmp_path: Path) -> None:
    """A command's exit code and stdout reach the metric; a nonzero exit is not a run failure (E38)."""
    metric = "import json\ndef score(repo_path, case, output):\n    return 0.5, json.dumps(output, sort_keys=True)\n"
    archive = _archive(
        tmp_path, {"src/app.py": "x = 1\n", "src/cmd.py": "import sys\nprint(sys.stdin.read())\nsys.exit(3)\n"}
    )
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=LocalSubprocessRuntime(),
            spec=SandboxSpec(lifetime_seconds=60, network_disabled=True),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
        ),
        metric,
        timeout_seconds=60,
        run_entry=("python3", "src/cmd.py"),
        run_timeout=10.0,
    )
    try:
        result = scorer.score("", {"input": {"q": 1}, "expected": 2})
    finally:
        scorer.close()
    assert result["score"] == pytest.approx(0.5)
    assert json.loads(result["feedback"]) == {"exit_code": 3, "stdout": '{"q": 1}\n'}


def test_without_a_run_entry_no_candidate_runs(tmp_path: Path) -> None:
    """A run without ``[run]`` scores the tree directly and passes no output (E38)."""
    metric = "import json\ndef score(repo_path, case, output):\n    return 1.0, json.dumps(output)\n"
    scorer = _probed(tmp_path, None, code=metric)
    try:
        result = scorer.score("", {"input": 1})
    finally:
        scorer.close()
    assert json.loads(result["feedback"]) is None


def test_a_metric_that_starts_a_process_after_the_run_is_refused(tmp_path: Path) -> None:
    """The metric may not rerun the candidate: a new process is refused, so the version has no score (E40)."""
    metric = "import subprocess\ndef score(repo_path, case, output):\n    subprocess.run(['true'])\n    return 1.0\n"
    scorer = _probed(tmp_path, "src/cand.py:echo", code=metric)
    try:
        with pytest.raises(RepoScoreError, match="raised an error"):
            scorer.score("", {"input": 1})
    finally:
        scorer.close()

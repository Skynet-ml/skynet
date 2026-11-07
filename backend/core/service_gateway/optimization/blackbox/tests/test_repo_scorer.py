"""Tests for scoring repository versions in the parent-owned box."""

from __future__ import annotations

import json
import logging
import time
import traceback
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from core.billing.signals import BudgetReached
from core.exceptions import ServiceError
from core.models.blackbox import BlackboxRunRequest
from core.models.tests.test_blackbox import _repo_request

from ..repo_constraints import TargetStage
from ..repo_scorer import (
    _STAGE_LAYOUT_SECONDS,
    SETUP_ALLOWANCE_SECONDS,
    RepoScoreError,
    RepoScorer,
    _scrub,
    _StageRelay,
    fold_case_scores,
)
from ..repo_workspace import RepoWorkspace
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
        "secret": os.environ["API_KEY"],
        "built": built,
        "case": case,
        "cases": {
            "small": {"score": value / 20, "feedback": "small ok"},
            "large": {"score": value / 5, "feedback": "large ok"},
        },
    }
"""


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


def test_secrets_reach_the_scorer_but_are_redacted(scored: tuple[RepoScorer, Path, Path]) -> None:
    """A scorer that echoes a secret never hands its value back."""
    scorer, _, _ = scored
    assert scorer.score("")["secret"] == "[secret]"


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
    leak = "import os\nopen(os.path.join('eval', os.environ['API_KEY']), 'w').close()\n"
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
                "reason": "'eval/labels.csv' is protected by the repository's optimize.toml ([paths] protected).",
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
            "Check the constraints file. You are probably about to break one of its rules:"
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
        "        values = list(pool.map(lambda value: call('slow', value), [0.6] * 4))\n"
        "    return sum(values), 'ok'\n"
    )
    stages = {"slow": TargetStage(entry="src/stages.py:slow", timeout=30)}
    runtime = RecordingRuntime()
    scorer, _ = _stage_scorer(tmp_path, code, runtime, stages, max_concurrency=4)
    try:
        started = time.monotonic()
        assert scorer.score("")["score"] == pytest.approx(2.4)
        assert time.monotonic() - started < 2.0
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

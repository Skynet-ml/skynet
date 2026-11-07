"""Tests for scoring repository versions in the parent-owned box."""

from __future__ import annotations

import json
import traceback
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import BlackboxRunRequest
from core.models.tests.test_blackbox import _repo_request

from ..repo_scorer import RepoScoreError, RepoScorer, _scrub, fold_case_scores
from ..repo_workspace import RepoWorkspace
from ..rule_judge import (
    EVENT_BLOCK,
    EVENT_NOTE,
    EVENT_VERDICT,
    ILLEGAL_ACTION_MESSAGE,
    WATCH_EVERY_ACTIONS,
    RuleJudge,
)
from ..sandbox import LocalSubprocessRuntime, SandboxSpec
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


def test_a_failing_scorer_is_reported(tmp_path: Path) -> None:
    """A scorer that raises surfaces its error as an unscorable version."""
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
        with pytest.raises(RepoScoreError, match="tests failed"):
            scorer.score("")
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


def _leaky(tmp_path: Path, code: str) -> RepoScorer:
    """Open a scorer whose metric names its own files and internals.

    Args:
        tmp_path: Scratch folder.
        code: Scorer source.

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
    """A scorer crash tells the agent the error, not the metric's file paths or lines (SKY-353)."""
    scorer = _leaky(
        tmp_path,
        "import os\ndef score(repo_path):\n    open(os.path.join(repo_path, 'eval/missing.csv'))\n",
    )
    try:
        reply = scorer.dispatch({"candidate": ""})
        assert reply.status == 422
        text = reply.body.decode()
        assert "eval/missing.csv" in text
        assert "Traceback" not in text
        assert 'File "' not in text
        assert "os.path.join" not in text
        assert ".app-repo" not in text
        assert str(tmp_path) not in text
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

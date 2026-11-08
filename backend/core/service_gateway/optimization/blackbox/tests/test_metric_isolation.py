"""Verify metric source never reaches an optimizer: native payloads, proposer workspaces and error feedback."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from core.exceptions import ServiceError

from .. import native_engines, native_runtime, runner
from ..native_runtime import NATIVE_TASK_FIELDS, assert_metric_absent, native_task_payload, run_native_engine
from ..protocol import EvalServer, Task
from ..sandbox import LocalSubprocessRuntime, SandboxSpec
from ..sandbox_scorer import RUNNER_FILE, SandboxPythonScorer
from .test_native_runtime import FakeRuntime, FakeSession, _context

SENTINEL = "METRIC_SENTINEL_7f3a9c"
VAL_SENTINEL = "VALSET_SENTINEL_51d0"
METRIC_CODE = (
    "def score(input, output, expected=None):\n"
    f"    marker = '{SENTINEL}'\n"
    "    return float(output == expected), {'feedback': 'compared'}\n"
)
TRACEBACK_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "<metric>", line 2, in score\n'
    f"    marker = '{SENTINEL}'\n"
    "ValueError: bad output"
)


def _task() -> Task:
    """Build a unified-like task whose private context holds the metric.

    Returns:
        A task with train cases, a validation set and the metric in context.
    """
    return Task(
        "seed",
        objective="Answer briefly",
        background="Support desk",
        cases=[{"input": {"q": "a"}, "expected": "A"}],
        valset=[{"input": {"q": VAL_SENTINEL}, "expected": "B"}],
        context={"metric_code": METRIC_CODE, "run_code": "def run(artifact, input):\n    return artifact\n"},
    )


def test_native_task_payload_holds_only_the_train_fields() -> None:
    """Ship seed, objective, background and train case inputs, never expected, context or validation cases."""
    task = _task()
    document = native_task_payload("meta_harness", task)

    assert set(document) == NATIVE_TASK_FIELDS
    assert document["train_set"] == [{"id": 0, "input": {"q": "a"}}]
    text = json.dumps(document)
    assert SENTINEL not in text
    assert VAL_SENTINEL not in text
    assert "metric_code" not in text


def test_native_payload_guard_refuses_a_document_with_the_metric() -> None:
    """Stop before writing a box file that would carry the metric source."""
    with pytest.raises(ServiceError, match="metric source"):
        assert_metric_absent(json.dumps({"leak": METRIC_CODE.strip()}), _task())
    assert_metric_absent(json.dumps({"fine": "seed"}), _task())


def test_error_side_info_is_one_line_without_source() -> None:
    """Reduce a scorer traceback to its ``Type: message`` line."""
    assert native_runtime.sanitize_side_info({"error": TRACEBACK_ERROR, "feedback": SENTINEL}) == {
        "error": "ValueError: bad output"
    }
    assert native_runtime.sanitize_side_info({"feedback": "ok"}) == {"feedback": "ok"}


def test_native_flow_never_writes_the_metric_into_the_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Grep every file a native engine's box receives, RPC answers included, for the metric.

    Args:
        tmp_path: Artifact destination.
        monkeypatch: Replaces the pinned source archive.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()

    def score(candidate: str, example: Any) -> tuple[float, dict[str, Any]]:
        """Fail the way a broken metric does, with its source in the error."""
        return 0.0, {"error": TRACEBACK_ERROR}

    run_native_engine("meta_harness", _task(), EvalServer(score, max_evals=3), _context(tmp_path, FakeRuntime(session)))

    assert session.files
    for path, text in session.files.items():
        assert SENTINEL not in text, path
        assert VAL_SENTINEL not in text, path
    [answer] = [text for path, text in session.files.items() if path.startswith("rpc/")]
    assert json.loads(answer)["info"] == {"error": "ValueError: bad output"}


def test_proposer_environment_drops_scorer_variables() -> None:
    """Keep the harness key and paths a proposer needs, drop scorer and nested-session variables."""
    env = native_engines.proposer_environment(
        {
            "PATH": "/bin",
            "APP_API_KEY": "route",
            "SKYNET_SCORER_DIR": "/private/scorer",
            "SKYNET_METRIC_PATH": "/private/metric.py",
            "CLAUDECODE": "1",
            "ANTHROPIC_API_KEY": "raw",
        }
    )

    assert env == {"PATH": "/bin", "APP_API_KEY": "route"}


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="Directory modes do not bind root.")
def test_local_proposer_workspace_cannot_find_scorer_files() -> None:
    """Search a proposer's whole reachable temp tree and environment for the scorer's metric."""
    scorer = SandboxPythonScorer(METRIC_CODE, runtime=LocalSubprocessRuntime(), gateway=None, timeout_seconds=30)
    proposer = LocalSubprocessRuntime().open(SandboxSpec(lifetime_seconds=60))
    try:
        scorer_box = scorer._open()
        scorer_dir = scorer_box.path
        assert (scorer_dir / runner.CODE_FILE).read_text(encoding="utf-8") == METRIC_CODE
        assert (scorer_dir / RUNNER_FILE).is_file()
        assert not scorer_dir.parent.samefile(proposer.path.parent)
        result = proposer.run(
            f"env; pwd; grep -rl {SENTINEL} {tempfile.gettempdir()} 2>/dev/null; true",
            timeout_seconds=60,
        )
        assert SENTINEL not in result.stdout
        assert str(scorer_dir) not in result.stdout
        assert str(scorer_dir.parent) not in result.stdout
    finally:
        proposer.close()
        scorer.close()


def test_scorer_calls_never_repeat_the_source() -> None:
    """Score through the real runner and confirm no call directory holds the metric."""
    scorer = SandboxPythonScorer(
        f"def score(candidate, case=None):\n    return 1.0, {{'feedback': 'ok {len(SENTINEL)}'}}  # {SENTINEL}\n",
        runtime=LocalSubprocessRuntime(),
        gateway=None,
        timeout_seconds=30,
    )
    try:
        assert scorer("x", {"input": "i"})[0] == 1.0
        assert scorer("y", {"input": "j"})[0] == 1.0
        calls = scorer._open().path / "calls"
        files = [path for path in calls.rglob("*") if path.is_file()]
        assert len(files) == 4
        for path in files:
            assert SENTINEL not in path.read_text(encoding="utf-8"), path
    finally:
        scorer.close()

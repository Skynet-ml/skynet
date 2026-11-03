"""The unified workspace scorer: one box, the harness inside, scores and side info out."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.models.unified import UnifiedRunRequest
from core.service_gateway.language_models import usage_by_model_from_history
from core.service_gateway.optimization.unified.metric_model import resolve_metric_model

from ..harness import GatewayConfig
from ..protocol import ScorerAbortError
from ..sandbox import LocalSubprocessRuntime, SandboxSession, SandboxSpec
from ..workspace_scorer import HARNESS_FILE, build_workspace_scorer
from .mocks import FakeGateway

RUN_CODE = "def run(artifact, input):\n    return artifact + ':' + input['q']\n"
SECRET = "SECRET_METRIC_MARKER_93"
METRIC_CODE = (
    f"# {SECRET}\n"
    "def score(input, output, expected=None):\n"
    "    return float(output == expected), {'feedback': 'compared'}\n"
)


def _payload(
    *, run_code: str = RUN_CODE, metric_code: str = METRIC_CODE, metric_model: str | None = None
) -> UnifiedRunRequest:
    """Build a pairs-mode prompt run on the blackbox metric shape."""
    payload = UnifiedRunRequest.model_validate(
        {
            "task": {"objective": "o", "artifact": {"kind": "prompt", "starting_version": "p"}},
            "data": {
                "mode": "pairs",
                "cases": [{"input": {"q": str(i)}, "expected": f"p:{i}"} for i in range(6)],
                "input_fields": ["q"],
            },
            "optimizer": {"choice": "best_of_n"},
            "models": {
                "reflection_model": {"name": "openrouter/task"},
                "metric_model": {"name": metric_model} if metric_model else None,
            },
            "workspace": {"run_code": run_code, "metric_code": metric_code, "metric_timeout_seconds": 30},
        }
    )
    assert resolve_metric_model(payload.models, payload.workspace.metric_code) is None
    return payload


class _RecordingRuntime(LocalSubprocessRuntime):
    """Local runtime that remembers its boxes' directories."""

    def __init__(self) -> None:
        """Start with no boxes."""
        self.dirs: list[Path] = []

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Open a local box and record where it lives."""
        session = super().open(spec)
        self.dirs.append(Path(session._dir))  # type: ignore[attr-defined]
        return session


def _files_containing(root: Path, needle: str) -> list[Path]:
    """Return every file under ``root`` whose text contains ``needle``."""
    return [p for p in root.rglob("*") if p.is_file() and needle in p.read_text(encoding="utf-8", errors="ignore")]


def test_scores_a_case_and_never_writes_the_metric_to_disk() -> None:
    """A passing case returns its score; no file in the box ever holds the metric source."""
    runtime = _RecordingRuntime()
    with FakeGateway(reply="unused") as gw:
        scorer = build_workspace_scorer(
            _payload(), GatewayConfig(url=gw.url, api_key="k"), protected=False, runtime=runtime
        )
        try:
            score, side_info = scorer("p", {"input": {"q": "1"}, "expected": "p:1"})
            [box] = runtime.dirs
            leaked = _files_containing(box, SECRET)
            assert (box / HARNESS_FILE).is_file()
        finally:
            scorer.close()

    assert (score, side_info["feedback"]) == (1.0, "compared")
    assert leaked == []


def test_one_box_serves_every_call() -> None:
    """The box opens lazily once and is reused."""
    runtime = _RecordingRuntime()
    with FakeGateway(reply="unused") as gw:
        scorer = build_workspace_scorer(
            _payload(), GatewayConfig(url=gw.url, api_key="k"), protected=False, runtime=runtime
        )
        try:
            assert runtime.dirs == []
            scorer("p", {"input": {"q": "1"}, "expected": "p:1"})
            scorer("p", {"input": {"q": "2"}, "expected": "nope"})
        finally:
            scorer.close()

    assert len(runtime.dirs) == 1


def test_warm_opens_the_box_before_the_first_call() -> None:
    """After ``warm`` the box exists; warming twice opens nothing more."""
    runtime = _RecordingRuntime()
    with FakeGateway(reply="unused") as gw:
        scorer = build_workspace_scorer(
            _payload(), GatewayConfig(url=gw.url, api_key="k"), protected=False, runtime=runtime
        )
        try:
            scorer.warm()
            assert len(runtime.dirs) == 1
            scorer.warm()
        finally:
            scorer.close()

    assert len(runtime.dirs) == 1


def test_run_many_scores_every_case_in_order_in_one_box() -> None:
    """The fan-out answers one probe per case, in order, from a single box."""
    runtime = _RecordingRuntime()
    cases = [{"input": {"q": str(i)}, "expected": f"p:{i}"} for i in range(3)]
    with FakeGateway(reply="unused") as gw:
        scorer = build_workspace_scorer(
            _payload(), GatewayConfig(url=gw.url, api_key="k"), protected=False, runtime=runtime
        )
        try:
            probes = scorer.run_many("p", cases)
            empty = scorer.run_many("p", [])
        finally:
            scorer.close()

    assert [(probe.score, probe.error) for probe in probes] == [(1.0, None)] * 3
    assert empty == []
    assert len(runtime.dirs) == 1


def test_user_error_reaches_the_optimizer_as_type_and_message_only() -> None:
    """A crash in the run code is ``(0.0, {"error": "Type: msg"})`` with no source or traceback."""
    run_code = "def run(artifact, input):\n    raise ValueError('bad input')\n"
    with FakeGateway(reply="unused") as gw:
        scorer = build_workspace_scorer(
            _payload(run_code=run_code),
            GatewayConfig(url=gw.url, api_key="k"),
            protected=False,
            runtime=LocalSubprocessRuntime(),
        )
        try:
            result = scorer("p", {"input": {"q": "1"}})
        finally:
            scorer.close()

    assert result == (0.0, {"error": "ValueError: bad input"})


def test_shape_mismatch_aborts_the_run() -> None:
    """A blackbox metric returning a bare float stops the run."""
    metric = "def score(input, output):\n    return 1.0\n"
    with FakeGateway(reply="unused") as gw:
        scorer = build_workspace_scorer(
            _payload(metric_code=metric),
            GatewayConfig(url=gw.url, api_key="k"),
            protected=False,
            runtime=LocalSubprocessRuntime(),
        )
        try:
            with pytest.raises(ScorerAbortError):
                scorer("p", {"input": {"q": "1"}})
        finally:
            scorer.close()


def test_run_and_metric_call_their_own_models_and_both_are_billed() -> None:
    """``llm`` is the task model in run code and the metric model in the metric."""
    run_code = "from skynet import llm\ndef run(artifact, input):\n    return llm(artifact, input['q'])\n"
    metric = (
        "from skynet import llm\n"
        "def score(input, output):\n"
        "    return float(llm('judge', output, model='openrouter/judge-model')), {'feedback': 'judged'}\n"
    )

    def reply(body: dict[str, Any]) -> str:
        """Answer the task model with text and the metric model with a number."""
        return "0.5" if body["model"] == "judge-model" else "generated"

    with FakeGateway(reply=reply, usage=(3, 1)) as gw:
        scorer = build_workspace_scorer(
            _payload(run_code=run_code, metric_code=metric, metric_model="openrouter/judge-model"),
            GatewayConfig(url=gw.url, api_key="k"),
            protected=False,
            runtime=LocalSubprocessRuntime(),
        )
        try:
            probe = scorer.run("prompt", {"input": {"q": "1"}})
        finally:
            scorer.close()
        models = [request["body"]["model"] for request in gw.requests]

    assert probe.score == 0.5
    assert probe.output_preview == "generated"
    assert models == ["task", "judge-model"]
    billed: dict[str, tuple[int, int]] = {}
    for ledger in scorer.ledgers:
        billed.update(usage_by_model_from_history(ledger))
    assert billed == {"openrouter/task": (3, 1), "openrouter/judge-model": (3, 1)}


def test_probe_carries_the_harness_observation_with_queue_and_evaluation_time() -> None:
    """The scorer adds the box wait and the whole boxed call to the harness's run-stage timing and cost."""
    run_code = "from skynet import llm\ndef run(artifact, input):\n    return llm(artifact, input['q'])\n"
    metric = "def score(input, output):\n    return 1.0, {'feedback': 'ok'}\n"
    with FakeGateway(reply="generated", cost=0.003) as gw:
        scorer = build_workspace_scorer(
            _payload(run_code=run_code, metric_code=metric),
            GatewayConfig(url=gw.url, api_key="k"),
            protected=False,
            runtime=LocalSubprocessRuntime(),
        )
        try:
            probe = scorer.run("prompt", {"input": {"q": "1"}})
            score, side_info, observed = scorer.score_observed("prompt", {"input": {"q": "2"}})
        finally:
            scorer.close()

    assert probe.observation is not None
    timing = probe.observation["timing"]
    assert timing["queue_ms"] >= 0
    assert timing["evaluation_ms"] >= timing["task_ms"] > 0
    assert probe.observation["cost"] == {
        "unit": "USD",
        "basis": "provider_usage",
        "coverage": "llm",
        "unpriced_calls": 0,
        "reason": None,
        "status": "metered",
        "usd": 0.003,
    }
    assert probe.observation["conditions"]["task_model"] == "task"
    assert (score, side_info["feedback"]) == (1.0, "ok")
    assert observed is not None
    assert observed["task_calls"] == 1


def test_build_rejects_unrouted_models_when_protected() -> None:
    """A protected box needs opaque routes for every model."""
    with pytest.raises(Exception, match="opaque"):
        build_workspace_scorer(
            _payload(),
            GatewayConfig(url="http://gw.example/v1", api_key="k"),
            protected=True,
            runtime=LocalSubprocessRuntime(),
        )

"""The unified workspace scorer: one box, the harness inside, scores and side info out."""

from __future__ import annotations

import contextvars
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from core.billing.signals import BudgetReached
from core.models.unified import UnifiedRunRequest
from core.sandbox import sync_bridge
from core.sandbox.timeouts import Phase
from core.service_gateway.language_models import usage_by_model_from_history
from core.service_gateway.optimization.unified.metric_model import resolve_metric_model

from .. import workspace_scorer as workspace_scorer_mod
from ..harness import GatewayConfig
from ..protocol import METRIC_ERROR, ScorerAbortError
from ..sandbox import LocalSubprocessRuntime, SandboxSession, SandboxSpec
from ..workspace_scorer import HARNESS_FILE, build_workspace_scorer, case_document
from .mocks import FakeGateway

PROGRAM = "import dspy\nprogram = dspy.Predict('q -> answer')\n"
RUN_CODE = (
    PROGRAM + "def run(artifact, input):\n"
    "    return dspy.Prediction(answer=program.signature.instructions + ':' + input['q'])\n"
)
SECRET = "SECRET_METRIC_MARKER_93"
CALLER_TAG: contextvars.ContextVar[str] = contextvars.ContextVar("caller_tag", default="unset")
METRIC_CODE = (
    f"# {SECRET}\n"
    "def score(input, output, expected=None):\n"
    "    return float(output.answer == expected), {'feedback': 'compared'}\n"
)


def _payload(
    *, run_code: str = RUN_CODE, metric_code: str = METRIC_CODE, metric_model: str | None = None
) -> UnifiedRunRequest:
    """Build a pairs-mode DSPy program run."""
    payload = UnifiedRunRequest.model_validate(
        {
            "task": {"objective": "o", "artifact": {"kind": "dspy_program", "starting_version": "p"}},
            "data": {
                "mode": "pairs",
                "cases": [{"input": {"q": str(i)}, "expected": f"p:{i}"} for i in range(6)],
                "input_fields": ["q"],
            },
            "optimizer": {"choice": "dspy_gepa"},
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


def test_the_candidate_never_gets_expected_from_a_bare_case() -> None:
    """A case with no ``input`` key reaches the candidate without its ``expected`` key; the metric still gets it."""
    run_code = PROGRAM + "def run(artifact, input):\n    return dspy.Prediction(answer=','.join(sorted(input)))\n"
    metric_code = (
        "def score(input, output, expected=None):\n    return 1.0, {'feedback': output.answer + '|' + str(expected)}\n"
    )
    with FakeGateway(reply="unused") as gw:
        scorer = build_workspace_scorer(
            _payload(run_code=run_code, metric_code=metric_code),
            GatewayConfig(url=gw.url, api_key="k"),
            protected=False,
            runtime=_RecordingRuntime(),
        )
        try:
            _, side_info = scorer("p", {"q": "2+2?", "expected": "4"})
        finally:
            scorer.close()

    assert side_info["feedback"] == "q|4"


@pytest.mark.parametrize(
    ("case", "document"),
    [
        ({"q": "2+2?", "expected": "4"}, {"input": {"q": "2+2?"}, "expected": "4"}),
        ({"q": "2+2?"}, {"input": {"q": "2+2?"}, "expected": None}),
        ({"input": None, "expected": "4"}, {"input": {}, "expected": "4"}),
        (
            {"input": {"q": "2+2?", "expected": "nested"}},
            {"input": {"q": "2+2?", "expected": "nested"}, "expected": None},
        ),
    ],
)
def test_case_document_keeps_expected_out_of_the_input(case: dict[str, Any], document: dict[str, Any]) -> None:
    """Only a top-level ``expected`` is a case's answer; a nested one is part of the input by design.

    Args:
        case: The case the engine passes.
        document: The form the harness reads.
    """
    assert case_document(case) == document


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
    """A crash in the run code is a metric error with no score, only ``Type: msg``, no source or traceback."""
    run_code = PROGRAM + "def run(artifact, input):\n    raise ValueError('bad input')\n"
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

    score, side_info = result
    assert score is None
    assert side_info == {"error": "ValueError: bad input", "status": METRIC_ERROR}


def test_a_metric_crash_has_no_score_in_the_final_run_path() -> None:
    """``score_observed`` (the final run's path) also returns no score and the metric error status."""
    metric = "def score(input, output):\n    raise RuntimeError('metric broke')\n"
    with FakeGateway(reply="unused") as gw:
        scorer = build_workspace_scorer(
            _payload(metric_code=metric),
            GatewayConfig(url=gw.url, api_key="k"),
            protected=False,
            runtime=LocalSubprocessRuntime(),
        )
        try:
            score, side_info, _ = scorer.score_observed("p", {"input": {"q": "1"}})
        finally:
            scorer.close()

    assert score is None
    assert side_info["status"] == METRIC_ERROR
    assert "RuntimeError" in side_info["error"]
    assert "metric broke" in side_info["error"]


def test_shape_mismatch_aborts_the_run() -> None:
    """A metric returning a bare float stops the run."""
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
    run_code = (
        "from skynet import llm\n" + PROGRAM + "def run(artifact, input):\n"
        "    return dspy.Prediction(answer=llm(program.signature.instructions, input['q']))\n"
    )
    metric = (
        "from skynet import llm\n"
        "def score(input, output):\n"
        "    return float(llm('judge', output.answer, model='openrouter/judge-model')), {'feedback': 'judged'}\n"
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
    assert probe.output_preview == '{"answer": "generated"}'
    assert models == ["task", "judge-model"]
    billed: dict[str, tuple[int, int]] = {}
    for ledger in scorer.ledgers:
        billed.update(usage_by_model_from_history(ledger))
    assert billed == {"openrouter/task": (3, 1), "openrouter/judge-model": (3, 1)}


def test_probe_carries_the_harness_observation_with_queue_and_evaluation_time() -> None:
    """The scorer adds the box wait and the whole boxed call to the harness's run-stage timing and cost."""
    run_code = (
        "from skynet import llm\n" + PROGRAM + "def run(artifact, input):\n"
        "    return dspy.Prediction(answer=llm(program.signature.instructions, input['q']))\n"
    )
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


@pytest.mark.parametrize("phase", [Phase.RUN, Phase.SCORE])
def test_run_on_pool_keeps_the_call_order_and_the_callers_context(phase: Phase) -> None:
    """Calls that end in reverse order still come back in the calls' order, each in the caller's context."""
    token = CALLER_TAG.set("caller")
    try:

        def call(n: int) -> tuple[int, str]:
            """Sleep less for later calls, so they end first."""
            time.sleep(0.01 * (4 - n))
            return n, CALLER_TAG.get()

        results = workspace_scorer_mod.run_on_pool(phase, [lambda n=n: call(n) for n in range(4)], 4)
    finally:
        CALLER_TAG.reset(token)

    assert results == [(n, "caller") for n in range(4)]


def test_run_on_pool_raises_the_earliest_failure_and_starts_no_more_calls() -> None:
    """A failed call stops the fan-out; its error reaches the caller unchanged."""
    started: list[int] = []
    lock = threading.Lock()

    def call(n: int) -> int:
        """Fail the first call; the others take long enough for the stop to land."""
        with lock:
            started.append(n)
        if n == 0:
            raise ValueError("boom")
        time.sleep(0.2)
        return n

    with pytest.raises(ValueError, match="boom"):
        workspace_scorer_mod.run_on_pool(Phase.SCORE, [lambda n=n: call(n) for n in range(6)], 2)

    assert set(started) <= {0, 1, 2}


def test_run_on_pool_cancel_waits_for_the_calls_in_flight_and_starts_no_more() -> None:
    """A cancel waits for the calls in flight to end, starts no queued call, and leaves the pool usable."""
    release = threading.Event()
    started = threading.Semaphore(0)
    ended = threading.Event()

    def call() -> int:
        """Block until the test lets the call end."""
        started.release()
        release.wait(timeout=10)
        return 1

    items = [(contextvars.copy_context(), call) for _ in range(3)]

    async def tracked() -> Any:
        """Run the fan-out two wide and mark when its task ends."""
        try:
            return await workspace_scorer_mod._run_on_pool(Phase.RUN, items, 2)
        finally:
            ended.set()

    future = sync_bridge.submit(tracked)
    assert started.acquire(timeout=5)
    assert started.acquire(timeout=5)
    future.cancel()
    time.sleep(0.05)
    assert not ended.is_set()
    release.set()

    assert ended.wait(timeout=5)
    assert not started.acquire(timeout=0.1)
    assert workspace_scorer_mod.run_on_pool(Phase.RUN, [lambda: 7], 1) == [7]


def test_run_on_pool_has_no_process_box_limit() -> None:
    """Two fan-outs of 12 hold 24 slot boxes open at once; no process limit makes either wait (SKY-542)."""
    width = 12
    barrier = threading.Barrier(2 * width, timeout=10)

    def call(n: int) -> int:
        """Return only once every call of both fan-outs is in flight at the same time."""
        barrier.wait()
        return n

    results: list[list[int]] = []
    fan_outs = [
        threading.Thread(
            target=lambda: results.append(
                workspace_scorer_mod.run_on_pool(Phase.SCORE, [lambda n=n: call(n) for n in range(width)], width)
            )
        )
        for _ in range(2)
    ]
    for thread in fan_outs:
        thread.start()
    for thread in fan_outs:
        thread.join(timeout=15)

    assert not barrier.broken
    assert results == [list(range(width)), list(range(width))]
    assert not hasattr(workspace_scorer_mod, "POOL_BOX_LIMIT")


async def _portal_echo() -> str:
    """Return a fixed value on the portal loop."""
    return "ok"


@pytest.mark.parametrize("stop", [BudgetReached, KeyboardInterrupt])
@pytest.mark.parametrize("phase", [Phase.RUN, Phase.SCORE])
def test_a_base_exception_in_a_call_reaches_the_caller_and_keeps_the_portal(
    phase: Phase, stop: type[BaseException]
) -> None:
    """BudgetReached or KeyboardInterrupt in a call reaches the caller; the portal thread lives on."""
    sync_bridge.run(_portal_echo)
    portal_thread = sync_bridge._thread
    assert portal_thread is not None

    def call() -> int:
        """Stop like a budget check or an interrupt in the call."""
        raise stop("stop")

    with pytest.raises(stop):
        workspace_scorer_mod.run_on_pool(phase, [call, lambda: 1], 2)

    time.sleep(0.1)
    assert portal_thread.is_alive()
    assert sync_bridge._thread is portal_thread
    assert sync_bridge.run(_portal_echo) == "ok"
    assert workspace_scorer_mod.run_on_pool(phase, [lambda: 7], 1) == [7]


def test_the_earliest_failure_wins_over_a_later_budget_stop() -> None:
    """A ValueError at index 0 that ends last still wins over BudgetReached at index 1."""
    later = threading.Event()

    def first() -> int:
        """Fail only after the second call stopped on budget."""
        later.wait(timeout=5)
        time.sleep(0.05)
        raise ValueError("first")

    def second() -> int:
        """Stop on budget at once."""
        later.set()
        raise BudgetReached("budget")

    with pytest.raises(ValueError, match="first"):
        workspace_scorer_mod.run_on_pool(Phase.RUN, [first, second], 2)


def test_run_on_pool_refuses_a_phase_other_than_run_or_score() -> None:
    """Pooled calls run only in the run or score phase."""
    with pytest.raises(ValueError, match="run or score"):
        workspace_scorer_mod.run_on_pool(Phase.SETUP, [lambda: 1], 1)

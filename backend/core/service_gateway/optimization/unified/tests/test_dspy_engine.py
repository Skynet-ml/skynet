"""Tests for the DSPy optimizers behind the black-box engine protocol."""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from core.exceptions import ServiceError
from core.models.unified import UnifiedCase
from core.service_gateway.optimization.blackbox import workspace_harness
from core.service_gateway.optimization.blackbox.protocol import (
    BudgetExhaustedError,
    EngineContext,
    EvalServer,
    ScorerAbortError,
    Task,
)
from core.service_gateway.optimization.blackbox.registry import ENGINES, available_engine_ids
from core.service_gateway.optimization.unified import dspy_engine as engine_mod
from core.service_gateway.optimization.unified.dspy_engine import (
    DSPY_STATE_KEY,
    EXPECTED_KEY,
    BudgetedMetric,
    DspyEngine,
    NoScore,
    RunProgram,
    apply_candidate,
    case_example,
    load_run_program,
    program_instructions,
)

# The one contract every artifact kind and optimizer runs: run(artifact,
# input) calling a module-level dspy.Module, scored by score(input, output, expected).
RUN_CODE = """
import dspy

program = dspy.Predict("question -> answer")

def run(artifact, input):
    return program(**input)
"""

SCORE_METRIC = """
def score(input, output, expected=None):
    ok = output.answer == expected["answer"]
    return float(ok), {"feedback": "right" if ok else "wrong"}
"""

# Returns the instruction of whichever module copy run actually called.
ECHO_RUN_CODE = """
import dspy

program = dspy.Predict("question -> answer")

def run(artifact, input):
    return program.signature.instructions + "|" + input["question"]
"""


def test_lm_helper_guards_model_and_forwards_call_overrides() -> None:
    """Forward sampling options without changing the metered model."""
    calls: list[dict[str, Any]] = []

    class LM:
        """Record a protected LM call."""

        budget_route = {"model": "openai/a"}

        def __call__(self, **kwargs: Any) -> list[str]:
            """Record call options and return a completion."""
            calls.append(kwargs)
            return ["ok"]

    llm = engine_mod.lm_helper(LM())
    assert llm("prompt", model="openrouter/openai/a", temperature=0.0, max_tokens=10, reasoning_effort="low") == "ok"
    with pytest.raises(engine_mod.helper_runtime.ScorerError, match="must match"):
        llm("prompt", model="openai/b")
    assert len(calls) == 1
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["max_tokens"] == 10
    assert calls[0]["reasoning_effort"] == "low"
    assert "model" not in calls[0]
    assert "extra_body" not in calls[0]


@pytest.mark.parametrize("model", ["litellm_proxy/anthropic/claude-sonnet-4", "openrouter/anthropic/claude-sonnet-4"])
def test_lm_helper_translates_new_gateway_reasoning(model: str) -> None:
    """Translate per-call effort for gateways without configured reasoning."""

    class LM:
        """Capture the request options."""

        kwargs: dict[str, Any] = {}

        def __init__(self) -> None:
            """Set the model under test and an empty call log."""
            self.model = model
            self.calls: list[dict[str, Any]] = []

        def __call__(self, **kwargs: Any) -> list[str]:
            """Record the request options and return a fixed completion."""
            self.calls.append(kwargs)
            return ["ok"]

    lm = LM()
    engine_mod.lm_helper(lm)("hi", reasoning_effort="high")
    assert lm.calls[0]["extra_body"]["reasoning"] == {"effort": "high", "summary": "auto"}
    lm.kwargs = {"extra_body": {"custom": 1, "reasoning": {"summary": "detailed", "effort": "low"}}}
    engine_mod.lm_helper(lm)("hi", reasoning_effort="high")
    assert lm.calls[1]["extra_body"] == {"custom": 1, "reasoning": {"summary": "detailed", "effort": "high"}}
    engine_mod.lm_helper(lm)("hi")
    assert "extra_body" not in lm.calls[2]


def test_lm_helper_forwards_explicit_effort_to_subscription_cli() -> None:
    """Replace a subscription's CLI effort only for an explicit helper override."""

    class LM:
        """Capture DSPy's merged request settings."""

        budget_route = {"model": "anthropic/claude-opus-5.5"}
        declared_model = "subscription/claude/anthropic/claude-opus-5.5"
        subscription = True
        kwargs = {"extra_body": {"skynet_cli_effort": "low", "other": True}}
        calls: list[dict[str, Any]] = []

        def __call__(self, **kwargs: Any) -> list[str]:
            """Record call settings and return a completion."""
            self.calls.append({**self.kwargs, **kwargs})
            return ["ok"]

    lm = LM()
    helper = engine_mod.lm_helper(lm)
    assert helper("judge", model=lm.declared_model, reasoning_effort="high") == "ok"
    assert helper("judge") == "ok"
    assert lm.calls[0]["extra_body"] == {"skynet_cli_effort": "high", "other": True}
    assert lm.calls[1]["extra_body"] == {"skynet_cli_effort": "low", "other": True}
    assert all("model" not in call for call in lm.calls)
    lm.kwargs = {}
    assert helper("judge", reasoning_effort="high") == "ok"
    assert lm.calls[2]["extra_body"] == {"skynet_cli_effort": "high"}
    assert helper("judge") == "ok"
    assert "extra_body" not in lm.calls[3]


@pytest.mark.parametrize("option", ["max_tokens", "reasoning_effort"])
def test_lm_helper_overrides_existing_reasoning_settings(option: str) -> None:
    """Replace reasoning-model defaults instead of sending conflicting settings."""

    class LM:
        """Merge call arguments as DSPy does."""

        budget_route = {"model": "openai/gpt-5-mini"}
        kwargs = {"max_completion_tokens": 1024, "extra_body": {"reasoning": {"effort": "high"}}}
        merged: dict[str, Any] = {}

        def __call__(self, **kwargs: Any) -> list[str]:
            """Record the merged request."""
            self.merged = {**self.kwargs, **kwargs}
            return ["ok"]

    lm = LM()
    engine_mod.lm_helper(lm)("prompt", **{option: 512 if option == "max_tokens" else "low"})
    if option == "max_tokens":
        assert lm.merged.get("max_completion_tokens") == 512
        assert "max_tokens" not in lm.merged
    else:
        assert lm.merged["reasoning_effort"] == lm.merged["extra_body"]["reasoning"]["effort"] == "low"


def _never_scored(*_args: Any) -> tuple[float, dict[str, Any]]:
    """Fail when the eval server's own scorer is called: the DSPy engine scores in-process.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError("the DSPy engine must not call the eval server's scorer")


def _cases(n: int, offset: int = 0) -> list[UnifiedCase]:
    """Build question/answer cases.

    Args:
        n: How many cases.
        offset: Index of the first case.

    Returns:
        Cases asking ``q<i>`` with the answer ``a``.
    """
    return [UnifiedCase(input={"question": f"q{i}"}, expected={"answer": "a"}) for i in range(offset, offset + n)]


def _ctx(tmp_path: Any) -> EngineContext:
    """Build an engine context whose reflection model is never needed.

    Args:
        tmp_path: Run directory.

    Returns:
        The context.
    """
    return EngineContext(reflection_lm=lambda prompt: "", run_dir=str(tmp_path))


def _task(train: list[UnifiedCase], val: list[UnifiedCase], *, metric_code: str = SCORE_METRIC) -> Task:
    """Build a BootstrapFewShot task around the dummy program.

    Args:
        train: Train cases.
        val: Validation cases.
        metric_code: The user metric.

    Returns:
        The task, with a dummy task model in its private context.
    """
    return Task(
        seed_candidate={"self": "Answer the question."},
        objective="answer",
        cases=list(train),
        valset=list(val),
        context={
            "run_code": RUN_CODE,
            "metric_code": metric_code,
            "input_fields": ["question"],
            "models": {"reflection_model": {"name": "openai/dummy"}},
            "dspy_kwargs": {"max_bootstrapped_demos": 1, "max_labeled_demos": 1},
            "task_lm": DummyLM([{"answer": "a"}] * 50),
        },
    )


def test_load_run_program_and_candidate_round_trip() -> None:
    """The run code's module-level module is found, and a candidate sets its instructions."""
    wrapper = load_run_program(RUN_CODE)
    assert isinstance(wrapper, RunProgram)
    apply_candidate(wrapper.program, {"self": "Be terse."})
    assert program_instructions(wrapper.program) == {"self": "Be terse."}


def test_load_run_program_requires_run() -> None:
    """Run code without run(artifact, input) is rejected, the retired build_program() included."""
    with pytest.raises(ServiceError, match="run"):
        load_run_program("import dspy\nprogram = dspy.Predict('q -> a')\n")
    with pytest.raises(ServiceError, match="run"):
        load_run_program("import dspy\ndef build_program():\n    return dspy.Predict('q -> a')\n")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("x = 1\n", "builds no dspy.Module"),
        ("a = dspy.Predict('q -> a')\nb = dspy.Predict('q -> a')\n", "several"),
    ],
)
def test_load_run_program_needs_one_identifiable_module(body: str, match: str) -> None:
    """No module, or several with none named ``program``, is a clear error.

    Args:
        body: Module-level statements of the run code.
        match: Text the error carries.
    """
    code = "import dspy\n" + body + "def run(artifact, input):\n    return None\n"
    with pytest.raises(ServiceError, match=match):
        load_run_program(code)


def test_several_modules_pick_the_one_named_program() -> None:
    """With several module-level modules, the one named ``program`` is optimized."""
    code = (
        "import dspy\n"
        "helper = dspy.Predict('q -> a')\n"
        "program = dspy.ChainOfThought('question -> answer')\n"
        "def run(artifact, input):\n    return program(**input)\n"
    )
    wrapper = load_run_program(code)
    assert isinstance(wrapper.program, dspy.ChainOfThought)


def test_every_copy_runs_through_run_with_its_own_module() -> None:
    """A deep copy the optimizer makes goes through ``run`` with the copy's module, not the original."""
    original = load_run_program(ECHO_RUN_CODE)
    apply_candidate(original.program, {"self": "first"})
    copy = original.deepcopy()
    apply_candidate(copy.program, {"self": "second"})

    first = original(question="q")
    second = copy(question="q")

    assert first[engine_mod.RUN_OUTPUT_KEY] == "first|q"
    assert second[engine_mod.RUN_OUTPUT_KEY] == "second|q"
    assert engine_mod.run_output(second) == "second|q"


def test_case_example_marks_inputs_and_keeps_expected() -> None:
    """A case becomes an example with the declared inputs and its expected value as it was."""
    example = case_example(_cases(1)[0], ["question"])
    assert example.question == "q0"
    assert example.answer == "a"
    assert example[EXPECTED_KEY] == {"answer": "a"}
    assert set(example.inputs().keys()) == {"question"}


def test_metric_gets_input_output_and_expected_like_the_harness() -> None:
    """DSPy's ``(gold, pred)`` call reaches ``score`` as the case input, run's output and the expected value."""
    server = EvalServer(_never_scored, max_evals=5)
    calls: list[tuple[Any, ...]] = []

    def score(input: Any, output: Any, expected: Any = None) -> Any:
        """Record the call."""
        calls.append((input, output, expected))
        return 1.0, {"feedback": "ok"}

    gold = case_example(UnifiedCase(input={"question": "q"}, expected="a"), ["question"])
    pred = dspy.Prediction(**{engine_mod.RUN_OUTPUT_KEY: "a"})
    assert BudgetedMetric(score, server, feedback=False)(gold, pred, None, "p", None) == 1.0
    assert calls == [({"question": "q"}, "a", "a")]


def test_metric_without_expected_gets_two_arguments() -> None:
    """``score(input, output)`` is called without the expected value."""
    server = EvalServer(_never_scored, max_evals=5)

    def score(input: Any, output: Any) -> Any:
        """Accept anything."""
        return 0.5, {"feedback": "half"}

    gold = case_example(_cases(1)[0], ["question"])
    assert BudgetedMetric(score, server, feedback=False)(gold, dspy.Prediction(answer="a")) == 0.5


def test_feedback_metric_returns_a_prediction_and_charges_the_budget() -> None:
    """For DSPy GEPA the score and feedback come back as a Prediction, and every call counts."""
    server = EvalServer(_never_scored, max_evals=2)

    def score(input: Any, output: Any, expected: Any = None) -> Any:
        """Return a score with feedback."""
        return 0.75, {"feedback": "close"}

    gold = case_example(_cases(1)[0], ["question"])
    wrapped = BudgetedMetric(score, server, feedback=True)
    first = wrapped(gold, dspy.Prediction(answer="a"))
    assert isinstance(first, dspy.Prediction)
    assert (first.score, first.feedback) == (0.75, "close")
    wrapped(gold, dspy.Prediction(answer="a"))
    assert server.used == 2
    with pytest.raises(BudgetExhaustedError):
        wrapped(gold, dspy.Prediction(answer="a"))
    assert wrapped.exhausted is True


def test_metric_with_the_wrong_return_shape_is_rejected() -> None:
    """``score`` must return ``(score, side_info)`` here exactly as in the harness."""
    server = EvalServer(_never_scored, max_evals=5)

    def score(input: Any, output: Any, expected: Any = None) -> Any:
        """Return a bare float."""
        return 1.0

    gold = case_example(_cases(1)[0], ["question"])
    with pytest.raises(ServiceError, match="side_info"):
        BudgetedMetric(score, server, feedback=False)(gold, dspy.Prediction(answer="a"))


def _raising_score(input: Any, output: Any, expected: Any = None) -> Any:
    """Crash like a broken user metric.

    Raises:
        ValueError: Always.
    """
    raise ValueError("metric blew up")


@pytest.mark.parametrize("feedback", [False, True])
def test_a_raising_metric_gives_no_score_and_is_charged_as_a_metric_error(feedback: bool) -> None:
    """SKY-367: a metric crash is charged with no score, and DSPy gets a falsy negative infinity, never 0."""
    scores: list[float | None] = []
    server = EvalServer(_never_scored, max_evals=5, on_eval=lambda _server, score: scores.append(score))
    gold = case_example(_cases(1)[0], ["question"])

    out = BudgetedMetric(_raising_score, server, feedback=feedback)(gold, dspy.Prediction(answer="a"))

    score = out.score if feedback else out
    assert isinstance(score, NoScore)
    assert score == float("-inf")
    assert not score
    assert not score >= 0.0
    if feedback:
        assert out.feedback == "ValueError: metric blew up"
    assert server.used == 1
    assert scores == [None]


@pytest.mark.parametrize("error", [ScorerAbortError("stop"), BudgetExhaustedError("spent")])
def test_a_metric_stop_still_stops_the_run(error: Exception) -> None:
    """A deliberate stop from inside the metric is not turned into a metric error."""
    server = EvalServer(_never_scored, max_evals=5)

    def score(input: Any, output: Any, expected: Any = None) -> Any:
        """Raise the stop."""
        raise error

    gold = case_example(_cases(1)[0], ["question"])
    with pytest.raises(type(error)):
        BudgetedMetric(score, server, feedback=False)(gold, dspy.Prediction(answer="a"))
    assert server.used == 0


CRASH_ON_Q0_METRIC = """
def score(input, output, expected=None):
    if input["question"] == "q0":
        raise ValueError("metric blew up")
    ok = output.answer == expected["answer"]
    return float(ok), {"feedback": "right" if ok else "wrong"}
"""


def test_bootstrap_survives_a_raising_metric_and_never_keeps_its_case_as_a_demo(tmp_path: Any) -> None:
    """The run completes, and the case the metric crashed on never becomes a few-shot demo.

    Args:
        tmp_path: Run directory.
    """
    server = EvalServer(_never_scored, max_evals=100)
    task = _task(_cases(3), _cases(2, offset=3), metric_code=CRASH_ON_Q0_METRIC)

    result = DspyEngine("dspy_bootstrap_fewshot").run(task, server, _ctx(tmp_path))

    program = load_run_program(RUN_CODE).program
    apply_candidate(program, result.best_candidate)
    assert program.demos
    assert all(demo["question"] != "q0" for demo in program.demos)
    assert result.metadata["metric_calls"] > 0


def test_engine_compiles_on_train_and_never_sees_other_cases(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """BootstrapFewShot compiles on train; the metric sees only the task's cases.

    Args:
        tmp_path: Run directory.
        monkeypatch: Pytest fixture recording the metric's calls.
    """
    train, val = _cases(4), _cases(2, offset=4)
    seen: list[str] = []
    real_load = engine_mod.load_score

    def load(code: str) -> Any:
        """Wrap the real metric so every case question it scores is recorded."""
        inner = real_load(code)

        def score(input: Any, output: Any, expected: Any = None) -> Any:
            """Record and delegate."""
            seen.append(input["question"])
            return inner(input, output, expected)

        return score

    monkeypatch.setattr(engine_mod, "load_score", load)
    task = _task(train, val)
    server = EvalServer(_never_scored, max_evals=100)
    result = DspyEngine("dspy_bootstrap_fewshot").run(task, server, _ctx(tmp_path))
    assert isinstance(result.best_candidate, dict)
    assert set(result.best_candidate) == {"self", DSPY_STATE_KEY}
    assert result.artifact is not None
    assert result.metadata["selection_scope"] == "validation"
    assert server.used == result.metadata["metric_calls"] > 0
    assert server.used == result.total_evals
    assert seen
    assert set(seen) <= {case.input["question"] for case in train + val}


def test_engine_candidate_carries_the_compiled_demos(tmp_path: Any) -> None:
    """The winning candidate rebuilds the compiled program, few-shot demos included.

    Args:
        tmp_path: Run directory.
    """
    server = EvalServer(_never_scored, max_evals=100)
    result = DspyEngine("dspy_bootstrap_fewshot").run(_task(_cases(4), _cases(2, offset=4)), server, _ctx(tmp_path))

    program = load_run_program(RUN_CODE).program
    assert program.demos == []
    apply_candidate(program, result.best_candidate)

    assert program.demos
    assert program.demos[0]["answer"] == "a"
    assert workspace_harness.DSPY_STATE_KEY == DSPY_STATE_KEY


def test_harness_scores_the_compiled_program_with_its_demos(tmp_path: Any) -> None:
    """The scorer harness loads the candidate's full state, so it runs the program that is saved.

    Args:
        tmp_path: Run directory.
    """
    server = EvalServer(_never_scored, max_evals=100)
    result = DspyEngine("dspy_bootstrap_fewshot").run(_task(_cases(4), _cases(2, offset=4)), server, _ctx(tmp_path))
    stage = workspace_harness.RunStage({"run_code": RUN_CODE, "artifact_kind": "dspy_program", "gateway": None})
    lm = DummyLM([{"answer": "a"}])

    with dspy.context(lm=lm):
        stage.run(result.best_candidate, {"question": "unseen"})

    demo_question = stage.program.demos[0]["question"]
    assert demo_question in str(lm.history[-1]["messages"])


def test_metric_llm_import_is_bound_to_the_metric_model(tmp_path: Any) -> None:
    """``from skynet import llm`` in the metric loads and calls the metered metric model.

    Args:
        tmp_path: Run directory.
    """
    metric_code = (
        "from skynet import llm\n"
        "def score(input, output, expected=None):\n"
        "    return float(llm('judge: ' + output.answer) == 'yes'), {'feedback': 'judged'}\n"
    )
    task = _task(_cases(2), [], metric_code=metric_code)
    metric_lm = DummyLM([{"response": "yes"}] * 50)
    task.context["metric_lm"] = metric_lm
    server = EvalServer(_never_scored, max_evals=100)

    result = DspyEngine("dspy_bootstrap_fewshot").run(task, server, _ctx(tmp_path))

    assert result.metadata["metric_calls"] > 0
    assert metric_lm.history


def test_engine_stays_within_the_scorer_budget(tmp_path: Any) -> None:
    """A budget smaller than the optimizer wants caps its metric calls and is reported.

    Args:
        tmp_path: Run directory.
    """
    server = EvalServer(_never_scored, max_evals=1)
    task = _task(_cases(4), [])
    task.context["dspy_kwargs"] = {"max_bootstrapped_demos": 4, "max_labeled_demos": 4}
    result = DspyEngine("dspy_bootstrap_fewshot").run(task, server, _ctx(tmp_path))
    assert result.metadata["budget_exhausted"] is True
    assert result.metadata["selection_scope"] == "training"
    assert server.used == 1


def test_unknown_dspy_optimizer_is_rejected() -> None:
    """Only the three DSPy optimizer ids build an engine."""
    with pytest.raises(ServiceError):
        DspyEngine("gepa")


def test_dspy_engines_are_registered_but_hidden_from_the_catalog() -> None:
    """The DSPy engines resolve by id yet never appear in the black-box engine list."""
    for engine_id in ("dspy_gepa", "dspy_miprov2", "dspy_bootstrap_fewshot"):
        assert ENGINES[engine_id].hidden is True
        assert engine_id not in available_engine_ids()


class _Reflection:
    """A reflection model whose DSPy LM proposes one fixed instruction."""

    def __init__(self) -> None:
        """Build the DSPy LM DSPy GEPA reflects with."""
        self.lm = DummyLM([{"new_instruction": "```\nAnswer with the single letter a.\n```"}] * 50)

    def __call__(self, prompt: Any) -> str:
        """Answer a black-box style reflection call; DSPy GEPA uses ``lm`` instead."""
        return ""


def test_dspy_gepa_compiles_the_same_run_code_with_score_feedback(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DSPy GEPA tunes the module run calls, with the same score() reaching it as feedback.

    Args:
        tmp_path: Run directory.
        monkeypatch: Pytest fixture recording what the metric returns to GEPA.
    """
    returned: list[Any] = []
    real_call = BudgetedMetric.__call__

    def record(self: BudgetedMetric, *args: Any, **kwargs: Any) -> Any:
        """Record what the wrapped metric hands DSPy GEPA."""
        out = real_call(self, *args, **kwargs)
        returned.append(out)
        return out

    monkeypatch.setattr(BudgetedMetric, "__call__", record)
    task = _task(_cases(3), _cases(2, offset=3))
    task.context["dspy_kwargs"] = {"max_metric_calls": 12}
    server = EvalServer(_never_scored, max_evals=12)
    ctx = EngineContext(reflection_lm=_Reflection(), run_dir=str(tmp_path))

    result = DspyEngine("dspy_gepa").run(task, server, ctx)

    assert isinstance(result.best_candidate, dict)
    assert "self" in result.best_candidate
    assert 0 < server.used <= 12
    assert returned
    assert all(isinstance(out, dspy.Prediction) and out.feedback in {"right", "wrong"} for out in returned)

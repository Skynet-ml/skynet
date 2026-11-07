"""Run the DSPy optimizers behind the black-box engine protocol.

A unified ``dspy_program`` run writes the same contract as every other
artifact: its run code defines ``run(artifact, input)`` and calls a
``dspy.Module`` it builds at module level. :class:`DspyEngine` loads that code
in-process (the worker already runs inside the run's outer sandbox), finds the
module, wraps ``run`` in a :class:`RunProgram` whose only sub-module is that
module, compiles the wrapper with the chosen DSPy optimizer on the train cases
(selecting on the validation cases) and hands back the module's per-predictor
instructions as the winning candidate plus the compiled module as an
artifact. Every case therefore goes through the user's own ``run``, exactly as
in the sandbox harness. The candidate also carries the compiled module's full
state (its few-shot demos included) under :data:`DSPY_STATE_KEY`, so the final
runs, the regression guard and the reported scores measure exactly the program
that is saved. Every metric call is charged to the run's eval server so the
scorer-run budget holds exactly as it does for the black-box engines.

The run code and the metric import their ``llm`` from a ``skynet`` helper
module, as they do in the sandbox harness: the run code's calls go to the
task model and the metric's to the metric model, both metered by the job.

The metric is the same ``score(input, output, expected=None)`` every engine
calls; :class:`BudgetedMetric` adapts DSPy's ``(gold, pred, ...)`` call to it.
The metric source stays here: the DSPy reflection model only ever sees the
feedback text the metric returns.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import threading
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import dspy
from gepa.oa.budget import BudgetExhausted

from ....billing.cli_dispatch import CLI_EFFORT_KEY
from ....exceptions import ServiceError
from ....models.common import ModelConfig
from ....models.unified import DSPY_OPTIMIZER_NAMES, METRIC_SIGNATURE, RUN_ENTRY
from ....registry import ResolverError, resolve_optimizer_factory
from ...language_models import build_language_model
from ..artifacts import persist_program
from ..blackbox import runner as helper_runtime
from ..blackbox import workspace_harness
from ..blackbox.protocol import (
    METRIC_ERROR,
    STATUS_KEY,
    BudgetExhaustedError,
    Candidate,
    EngineContext,
    EvalServer,
    Result,
    ScorerAbortError,
    Task,
)
from ..cost_ceiling import CostCeilingExceededError
from ..data import DatasetSplits
from ..optimizers import compile_program, instantiate_optimizer

logger = logging.getLogger(__name__)

_GEPA_BUDGET_KEYS = ("auto", "max_full_evals", "max_metric_calls")
# Only DSPy GEPA reflects on feedback text; the others read the score alone.
_FEEDBACK_OPTIMIZER = "dspy_gepa"
# Only GEPA has a resolver alias; the other DSPy names resolve by dotted path.
_FACTORY_PATHS = {"miprov2": "dspy.MIPROv2", "bootstrapfewshot": "dspy.BootstrapFewShot"}
# A candidate is ``dict[str, str]``; this key holds the compiled program's
# state as JSON next to the per-predictor instructions.
DSPY_STATE_KEY = workspace_harness.DSPY_STATE_KEY
# The case's expected value as it was, next to the label fields a DSPy demo
# reads, so ``score`` gets exactly what the sandbox harness hands it. This key
# and ``RUN_OUTPUT_KEY`` keep their pre-rename names: demos copy example and
# prediction fields into saved program state, which later runs load again.
EXPECTED_KEY = "_skynet_expected"
# Holds what ``run`` returned when it is not a ``dspy.Prediction``.
RUN_OUTPUT_KEY = "_skynet_output"
_ACTIVE_PROGRAM: contextvars.ContextVar[Any] = contextvars.ContextVar("app_active_program", default=None)


def program_state(program: dspy.Module) -> str:
    """Serialize a program's full state (instructions and demos), without per-predictor LMs.

    Args:
        program: A DSPy module.

    Returns:
        The state as JSON text.
    """
    state = program.dump_state()
    for entry in state.values():
        if isinstance(entry, dict) and "lm" in entry:
            # The predictor's own LM would override the metered one the scorer binds.
            entry["lm"] = None
    return json.dumps(state, default=str)


def lm_helper(lm: Any) -> Callable[..., str]:
    """Wrap a metered DSPy LM as the ``llm`` helper user code calls.

    Args:
        lm: A ``dspy.LM``, or ``None`` when no model was configured.

    Returns:
        ``llm(prompt=None, input=None, *, images=None, messages=None) -> str``.
    """
    if lm is None:
        return helper_runtime.missing_llm

    def llm(
        prompt: Any = None,
        input: Any = None,
        *,
        images: Any = None,
        messages: Any = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Complete one request through the metered LM and return its text.

        Args:
            prompt: The text to run. With ``input`` it is the system message;
                alone it is the user message.
            input: The case's input, sent as the user message when given.
            images: One image or a list of images for the user message.
            messages: Chat messages to send as they are, in place of
                ``prompt``, ``input`` and ``images``.
            model: The model the code declares. It only checks the bound
                model and never changes the model that is called and billed.
            temperature: Sampling temperature for this call, or ``None`` for
                the LM default.
            max_tokens: Output token limit for this call, or ``None`` for the
                LM default. It is sent as ``max_completion_tokens`` when the
                LM uses that key.
            reasoning_effort: Reasoning effort for this call, or ``None`` for
                the LM default. It goes in the request body only for a gateway
                route, a native reasoning setting, or a CLI subscription.

        Returns:
            The text of the first completion, or an empty string.

        Raises:
            ScorerError: ``model`` does not match the bound or declared
                model, or there is neither a prompt nor messages.
        """
        route = getattr(lm, "budget_route", None)
        bound = route.get("model") if isinstance(route, dict) else getattr(lm, "model", "")
        declared = getattr(lm, "declared_model", None)
        if model is not None and not helper_runtime.model_matches_bound(model, str(bound), declared):
            raise helper_runtime.ScorerError("llm(model=...) must match the model bound to this code.")
        if messages is None:
            if prompt is None:
                raise helper_runtime.ScorerError("llm() needs a prompt, or messages=[...].")
            messages = helper_runtime.scorer_messages(prompt, input, images)
        overrides = {
            key: value
            for key, value in (
                ("temperature", temperature),
                ("max_tokens", max_tokens),
                ("reasoning_effort", reasoning_effort),
            )
            if value is not None
        }
        defaults = getattr(lm, "kwargs", {})
        if isinstance(defaults, dict):
            if max_tokens is not None and "max_completion_tokens" in defaults:
                overrides.pop("max_tokens", None)
                overrides["max_completion_tokens"] = max_tokens
            if reasoning_effort is not None:
                raw_body = defaults.get("extra_body")
                body = dict(raw_body) if isinstance(raw_body, dict) else {}
                reasoning = body.get("reasoning")
                model_id = getattr(lm, "model", "")
                gateway = isinstance(model_id, str) and model_id.startswith(("litellm_proxy/", "openrouter/"))
                if isinstance(reasoning, dict) or gateway:
                    native = dict(reasoning) if isinstance(reasoning, dict) else {}
                    native["effort"] = reasoning_effort
                    if gateway:
                        native.setdefault("summary", "auto")
                    body["reasoning"] = native
                if getattr(lm, "subscription", False):
                    body[CLI_EFFORT_KEY] = reasoning_effort
                if isinstance(reasoning, dict) or gateway or getattr(lm, "subscription", False):
                    overrides["extra_body"] = body
        outputs = lm(messages=messages, **overrides)
        first = outputs[0] if outputs else ""
        return str(first.get("text") or "") if isinstance(first, dict) else str(first)

    return llm


def helper_module(lm: Any) -> types.ModuleType:
    """Build the ``skynet`` module one piece of user code imports from.

    Args:
        lm: The metered DSPy LM this code's ``llm`` calls, or ``None``.

    Returns:
        The helper module.
    """
    return helper_runtime.helper_module(
        {
            helper_runtime.LLM_HELPER_NAME: lm_helper(lm),
            helper_runtime.IMAGE_HELPER_NAME: helper_runtime.Image,
            helper_runtime.LOG_HELPER_NAME: helper_runtime.log,
        }
    )


@contextmanager
def bound_helpers(module: types.ModuleType | None) -> Iterator[None]:
    """Make ``import skynet`` resolve to ``module`` while user code loads.

    Args:
        module: The helper module, or ``None`` to leave ``skynet`` as it is.

    Yields:
        Nothing; the previous binding is restored on exit.
    """
    if module is None:
        yield
        return
    name = helper_runtime.HELPER_MODULE_NAME
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


class _ProgramProxy:
    """Stand in for the run code's module so ``run`` calls whichever copy the optimizer is scoring.

    DSPy optimizers deep-copy the program they tune, but ``run`` keeps reading
    the module from its own globals. The proxy sits in those globals and
    forwards every call and attribute to the copy :class:`RunProgram` marks
    active for the current thread.
    """

    def __init__(self, default: dspy.Module) -> None:
        """Remember the module the run code built.

        Args:
            default: The module, used whenever no copy is active.
        """
        self._default = default

    def _target(self) -> dspy.Module:
        """Return the module copy active in this thread, else the original."""
        return _ACTIVE_PROGRAM.get() or self._default

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the active module.

        Args:
            *args: Positional arguments for the module.
            **kwargs: Keyword arguments for the module.

        Returns:
            What the module returned.
        """
        return self._target()(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Read an attribute of the active module.

        Args:
            name: The attribute.

        Returns:
            Its value on the active module.
        """
        return getattr(self._target(), name)


class RunProgram(dspy.Module):
    """The user's ``run(artifact, input)`` as a DSPy module whose only sub-module is the module ``run`` calls.

    The optimizer tunes ``program``'s predictors; every forward pass marks
    this instance's ``program`` active and goes through ``run``, so the
    output the metric sees is the output ``run`` returns.
    """

    def __init__(self, run: Callable[..., Any], program: dspy.Module) -> None:
        """Wrap the run function around the module it calls.

        Args:
            run: The user's ``run(artifact, input)``.
            program: The module-level ``dspy.Module`` the run code built.
        """
        super().__init__()
        self.program = program
        self._run = run

    def forward(self, **inputs: Any) -> dspy.Prediction:
        """Run one case through the user's ``run`` with this instance's module.

        Args:
            **inputs: The case's input fields.

        Returns:
            The ``dspy.Prediction`` ``run`` returned, or one holding its output
            under :data:`RUN_OUTPUT_KEY`.
        """
        token = _ACTIVE_PROGRAM.set(self.program)
        try:
            output = self._run(program_instructions(self.program), dict(inputs))
        finally:
            _ACTIVE_PROGRAM.reset(token)
        if isinstance(output, dspy.Prediction):
            return output
        return dspy.Prediction(**{RUN_OUTPUT_KEY: output})


def load_run_program(run_code: str, helpers: types.ModuleType | None = None) -> RunProgram:
    """Execute the run code and wrap its ``run`` around the ``dspy.Module`` it calls.

    Args:
        run_code: The workspace run code.
        helpers: The ``skynet`` module the run code imports from, if any.

    Returns:
        A fresh :class:`RunProgram`.

    Raises:
        ServiceError: When the code does not compile, defines no ``run`` or
            builds no module-level ``dspy.Module`` (or several, none named
            ``program``).
    """
    namespace: dict[str, Any] = {"dspy": dspy, "__name__": "app_run"}
    with bound_helpers(helpers):
        try:
            # exec: user run code, inside the per-job worker that already runs in
            # the run's outer sandbox. dont_inherit keeps this module's future
            # import from stringizing the user's annotations.
            exec(compile(run_code, "<run_code>", "exec", dont_inherit=True), namespace)
        except SyntaxError as exc:
            raise ServiceError(f"run_code has a syntax error: {exc}") from exc
    run = namespace.get(workspace_harness.RUN_ENTRY_NAME)
    if not callable(run):
        raise ServiceError(f"run_code must define {RUN_ENTRY}.")
    try:
        program = workspace_harness.find_program(namespace, dspy.Module)
    except NameError as exc:
        raise ServiceError(str(exc)) from exc
    proxy = _ProgramProxy(program)
    for name, value in list(namespace.items()):
        if value is program:
            namespace[name] = proxy
    return RunProgram(run, program)


def program_instructions(program: dspy.Module) -> dict[str, str]:
    """Read each predictor's instruction, keyed by predictor name.

    Args:
        program: A DSPy module.

    Returns:
        ``{predictor name: instruction}`` in the program's predictor order.
    """
    return {name: str(predictor.signature.instructions) for name, predictor in program.named_predictors()}


def apply_candidate(program: dspy.Module, candidate: Candidate) -> None:
    """Set the program's state and predictor instructions from a candidate, as the harness does.

    Args:
        program: The DSPy module to update in place.
        candidate: ``{predictor name: instruction}``, optionally with the full
            program state under :data:`DSPY_STATE_KEY`; a plain string sets
            every predictor's instruction.
    """
    workspace_harness.apply_dspy_candidate(program, candidate)


def case_example(case: Any, input_fields: list[str]) -> dspy.Example:
    """Turn one unified case into the example a DSPy optimizer trains on.

    Args:
        case: ``{"input": {...}, "expected": {...}}`` (or a ``UnifiedCase``).
        input_fields: The program's input field names; the case's input keys when empty.

    Returns:
        ``dspy.Example(**input, **expected).with_inputs(*input_fields)``, with
        the expected value as it was under :data:`EXPECTED_KEY`.
    """
    data = case.model_dump() if hasattr(case, "model_dump") else dict(case)
    inputs = dict(data.get("input") or {})
    expected = data.get("expected")
    outputs = expected if isinstance(expected, dict) else ({} if expected is None else {"expected": expected})
    return dspy.Example(**inputs, **outputs, **{EXPECTED_KEY: expected}).with_inputs(*(input_fields or list(inputs)))


def load_score(metric_code: str) -> Callable[..., Any]:
    """Execute the metric code and return its ``score`` function.

    Args:
        metric_code: The workspace metric code.

    Returns:
        The user's ``score(input, output, expected=None)``.

    Raises:
        ServiceError: When the code does not compile or defines no ``score``.
    """
    namespace: dict[str, Any] = {"dspy": dspy, "__name__": "app_metric"}
    try:
        # exec: user metric code, in the same per-job worker as the run code.
        exec(compile(metric_code, "<metric_code>", "exec", dont_inherit=True), namespace)
    except SyntaxError as exc:
        raise ServiceError(f"metric_code has a syntax error: {exc}") from exc
    score = namespace.get(workspace_harness.METRIC_ENTRY_NAME)
    if not callable(score):
        raise ServiceError(f"metric_code must define {METRIC_SIGNATURE}.")
    return score


class NoScore(float):
    """The score DSPy gets for a case the metric crashed on (SKY-367).

    DSPy needs a number for every case. Negative infinity keeps every mean
    that takes it in at negative infinity, so the version ranks below every
    scored one. It is falsy, so bootstrapping never keeps the case as a demo.
    """

    def __new__(cls) -> NoScore:
        """Make the negative infinity value.

        Returns:
            A new ``NoScore``.
        """
        return super().__new__(cls, float("-inf"))

    def __bool__(self) -> bool:
        """Read as a failed case wherever DSPy tests the score for truth.

        Returns:
            Always ``False``.
        """
        return False

    def __reduce__(self) -> tuple[type[NoScore], tuple[()]]:
        """Rebuild through the no-argument constructor on copy and pickle.

        The default float reduction passes the value to ``__new__``, which
        takes no value, so copies and pickles would fail without this.

        Returns:
            The class and its empty constructor arguments.
        """
        return (NoScore, ())


def run_output(pred: Any) -> Any:
    """Return what ``run`` returned for a prediction :class:`RunProgram` made.

    Args:
        pred: The prediction DSPy hands the metric.

    Returns:
        ``run``'s own return value.
    """
    if isinstance(pred, dspy.Prediction) and RUN_OUTPUT_KEY in pred:
        return pred[RUN_OUTPUT_KEY]
    return pred


class BudgetedMetric:
    """Adapt the user's ``score(input, output, expected=None)`` to DSPy and charge every call to the budget.

    DSPy calls ``metric(gold, pred, ...)``; the wrapper hands ``score`` the
    case input, what ``run`` returned and the case's expected value, exactly
    as the sandbox harness does. For DSPy GEPA it returns
    ``dspy.Prediction(score, feedback)`` so the reflection model reads the
    feedback; for the other optimizers it returns the float.
    """

    def __init__(self, metric: Callable[..., Any], server: EvalServer, *, feedback: bool) -> None:
        """Bind the user metric to the run's budget.

        Args:
            metric: The user's ``score`` function.
            server: The run's eval server.
            feedback: Whether the optimizer consumes feedback text.
        """
        self._metric = metric
        self._server = server
        self._feedback = feedback
        self._takes_expected = workspace_harness.positional_capacity(metric) >= 3
        self._lock = threading.Lock()
        self.exhausted = False
        self.calls = 0

    def __call__(self, gold: Any, pred: Any, trace: Any = None, pred_name: Any = None, pred_trace: Any = None) -> Any:
        """Score one prediction and charge it to the budget.

        Args:
            gold: The case as a ``dspy.Example`` (see :func:`case_example`).
            pred: The prediction :class:`RunProgram` returned.
            trace: DSPy's trace, when the optimizer passes one.
            pred_name: The predictor GEPA asks feedback for, if any.
            pred_trace: That predictor's trace, if any.

        Returns:
            ``dspy.Prediction(score, feedback)`` for DSPy GEPA, otherwise the
            score. A case the metric crashed on gets :class:`NoScore`.

        Raises:
            BudgetExhaustedError: When the scorer-run budget is spent.
            ServiceError: When ``score`` does not return ``(score, side_info)``.
        """
        if self.exhausted or self._server.remaining <= 0:
            self.exhausted = True
            raise BudgetExhaustedError("scorer-run budget exhausted")
        inputs = gold.inputs().toDict()
        output = run_output(pred)
        try:
            if self._takes_expected:
                raw = self._metric(inputs, output, gold.get(EXPECTED_KEY))
            else:
                raw = self._metric(inputs, output)
        except (ScorerAbortError, BudgetExhaustedError, BudgetExhausted, CostCeilingExceededError):
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("metric raised on a case (%s): %s", METRIC_ERROR, error)
            self._charge(None, {"error": error, STATUS_KEY: METRIC_ERROR})
            return dspy.Prediction(score=NoScore(), feedback=error) if self._feedback else NoScore()
        try:
            score, side_info = workspace_harness.normalize_metric(raw)
        except workspace_harness.ShapeError as exc:
            raise ServiceError(f"{METRIC_SIGNATURE} must return (score, side_info): {exc}") from exc
        self._charge(score, side_info)
        return dspy.Prediction(score=score, feedback=side_info["feedback"]) if self._feedback else score

    def _charge(self, score: float | None, side_info: dict[str, Any]) -> None:
        """Count one metric call against the run's budget.

        Args:
            score: The case's score, or ``None`` after a metric error.
            side_info: What the metric said about the case.

        Raises:
            BudgetExhaustedError: When the scorer-run budget is spent.
        """
        try:
            self._server.charge(None, score, side_info)
        except BudgetExhaustedError:
            self.exhausted = True
            raise
        with self._lock:
            self.calls += 1


def _model_config(raw: Any) -> ModelConfig | None:
    """Read a model config from the task context.

    Args:
        raw: A ``ModelConfig``, its dump, or ``None``.

    Returns:
        The model config, or ``None``.
    """
    if raw is None or isinstance(raw, ModelConfig):
        return raw
    return ModelConfig.model_validate(raw)


class DspyEngine:
    """One DSPy optimizer (``dspy_gepa``, ``dspy_miprov2``, ``dspy_bootstrap_fewshot``) as an engine."""

    def __init__(self, optimizer_id: str) -> None:
        """Pick the DSPy optimizer this engine runs.

        Args:
            optimizer_id: A key of :data:`DSPY_OPTIMIZER_NAMES`.

        Raises:
            ServiceError: When the id is not a DSPy optimizer.
        """
        if optimizer_id not in DSPY_OPTIMIZER_NAMES:
            raise ServiceError(f"Unknown DSPy optimizer '{optimizer_id}'.")
        self.name = optimizer_id

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Compile the workspace program on the train cases, selecting on the validation cases.

        Args:
            task: Train cases, validation cases and the private run context.
            server: The run's eval server; every metric call is charged to it.
            ctx: Reflection model and workspace.

        Returns:
            The compiled program's instructions as the best candidate and the
            program itself as ``artifact``; the starting point when the
            budget ran out before the optimizer finished.

        Raises:
            ServiceError: When the program, metric or optimizer cannot be built.
        """
        context = task.context
        optimizer_name = DSPY_OPTIMIZER_NAMES[self.name]
        models = context.get("models") or {}
        reflection_model = _model_config(models.get("reflection_model"))
        task_lm = context.get("task_lm")
        if task_lm is None:
            task_model = _model_config(models.get("task_model")) or reflection_model
            if task_model is None:
                raise ServiceError("A DSPy program needs a task model.")
            task_lm = build_language_model(task_model)
        program = load_run_program(str(context["run_code"]), helper_module(task_lm))
        if task.seed_candidate is not None:
            apply_candidate(program.program, task.seed_candidate)
        seed_candidate = program_instructions(program.program)
        input_fields = list(context.get("input_fields") or [])
        splits = DatasetSplits(
            train=[case_example(case, input_fields) for case in task.cases],
            val=[case_example(case, input_fields) for case in task.valset],
            test=[],
        )
        # Module-level ``from skynet import llm`` binds when the code loads, so
        # each piece of code keeps the model it was loaded with.
        with bound_helpers(helper_module(context.get("metric_lm"))):
            user_metric = load_score(str(context["metric_code"]))
        metric = BudgetedMetric(user_metric, server, feedback=self.name == _FEEDBACK_OPTIMIZER)
        try:
            factory = resolve_optimizer_factory(_FACTORY_PATHS.get(optimizer_name, optimizer_name))
        except ResolverError as exc:
            raise ServiceError(str(exc)) from exc
        kwargs = dict(context.get("dspy_kwargs") or {})
        if self.name == "dspy_gepa" and not any(key in kwargs for key in _GEPA_BUDGET_KEYS):
            kwargs["max_metric_calls"] = server.remaining
        reflection_lm = getattr(ctx.reflection_lm, "lm", None)
        if optimizer_name == "miprov2" and reflection_lm is not None:
            kwargs.setdefault("prompt_model", reflection_lm)
        optimizer = instantiate_optimizer(
            factory,
            optimizer_name,
            kwargs,
            metric,
            reflection_model,
            reflection_lm=reflection_lm,
            log_dir=ctx.run_dir,
        )
        logger.info(
            "DSPy %s: %d train / %d validation case(s), budget %d metric call(s)",
            optimizer_name,
            len(splits.train),
            len(splits.val),
            server.remaining,
        )
        try:
            with dspy.context(lm=task_lm):
                compiled = compile_program(
                    optimizer=optimizer, program=program, splits=splits, metric=metric, compile_kwargs={}
                )
        except BudgetExhaustedError:
            compiled = None
        except Exception:
            if not metric.exhausted:
                raise
            compiled = None
        selection = "validation" if splits.val else "training"
        if compiled is None:
            logger.info("DSPy %s ran out of scorer runs; keeping the starting point", optimizer_name)
            return Result(
                best_candidate=seed_candidate,
                best_score=None,
                total_evals=server.used,
                metadata={"selection_scope": selection, "metric_calls": metric.calls, "budget_exhausted": True},
            )
        tuned = compiled.program
        artifact = persist_program(tuned, None)
        return Result(
            best_candidate={**program_instructions(tuned), DSPY_STATE_KEY: program_state(tuned)},
            best_score=None,
            total_evals=server.used,
            metadata={
                "selection_scope": selection,
                "metric_calls": metric.calls,
                "budget_exhausted": metric.exhausted,
                "dspy_optimizer": optimizer_name,
            },
            artifact=artifact.model_dump(mode="json") if artifact is not None else None,
        )

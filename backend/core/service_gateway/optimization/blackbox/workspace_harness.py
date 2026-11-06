"""In-sandbox harness for unified workspace code: the run code, then the metric.

This file is shipped verbatim into the scorer sandbox as
``app_workspace_harness.py``, next to ``app_runner.py``, whose gateway
client, helper module and image wrapper it reuses. Like the runner it depends
on the standard library alone (``dspy`` is imported only for a DSPy program)
and stays valid Python 3.9.

Every artifact kind runs one contract: the run code defines
``run(artifact, input)`` and the metric defines ``score(input, output,
expected=None)`` returning ``(score, side_info)``. For a DSPy program the run
code builds its ``dspy.Module`` at module level and calls it inside ``run``;
the harness finds that module (:func:`find_program`), applies the candidate's
instructions and state to it and binds the task model as its LM.

Protocol: JSON lines. The first message initializes the workspace::

    {"run_code": str, "metric_code": str, "files": {path: str}, "artifact_kind": str,
     "input_fields": [str], "gateways": {"task": {...} | null, "metric": {...} | null}}

and is answered with ``{"ready": true}`` or a load error. Every later
``{"candidate": ..., "case": {"input": {...}, "expected": ...}}`` is answered
with ``{"output_preview", "score", "side_info", "usage"}`` or
``{"error", "stage": "run" | "metric" | "shape", "detail", "usage"}``.
``error`` is ``"<Type>: <message>"`` and is all an optimizer may ever see;
``detail`` adds the user-code frames for the person who wrote the code.
Every reply to a call that reached the run stage also carries
``"observation"`` (see :func:`build_observation`).

Trusted observations: this process, not the run child, measures each
evaluation. The run child's task-model calls go through :class:`TaskRelay`, a
loopback endpoint here that forwards each attempt to the real gateway and
records its status, tokens, provider cost and time; the child never holds the
task key or the upstream address. The run time starts when the child says it
loaded the run code (:data:`RUN_STARTED_MARKER`, the first one counts) and
ends when its last reply arrives. The metric reads the result through
``from skynet import observation, service_cost`` before it returns its score,
so nothing the run code or the candidate prints or returns can change it.
``from skynet import reward`` blends accuracy with that time and cost under the
metric's fixed ``REWARD_SPEC`` (see :func:`blend_reward`).

The run code and the candidate never share a process with the metric: each
call runs them in a fresh child interpreter (``--run-child``) that receives
only the run code, its extra workspace files, the task model, the candidate
and the case's inputs, and answers with the output as JSON. The extra files
(helpers ``run.py`` imports) are written to a fresh temporary directory at the
front of the child's ``sys.path`` and removed when the child is done; the
metric's process never sees them. Candidate-written code (a ``code`` artifact
the run code executes) therefore cannot read the metric source, the
expected value or the metric model's key, and a run-stage ``error`` is cut to
one short line.

The run and metric code arrive only inside the message stream and are never
written to a file. ``python3 app_workspace_harness.py`` reads the stream from stdin;
with ``--env`` it reads it from base64 chunks in ``APP_WS_STREAM_<n>``
environment variables, because the sandbox API runs one command at a time
without stdin, and drops them from the environment before user code runs.
Replies go to the original stdout prefixed with :data:`REPLY_PREFIX`; user
prints go to stderr.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import importlib
import importlib.util
import inspect
import json
import linecache
import math
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO

try:
    from . import runner as _runner
except ImportError:
    try:
        import app_runner as _runner  # type: ignore[no-redef]
    except ImportError:
        # The run child is this file run as a script from the package
        # directory (tests, local runs), where the runner is ``runner.py``.
        _spec = importlib.util.spec_from_file_location("app_runner", Path(__file__).resolve().parent / "runner.py")
        _runner = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type,no-redef]
        sys.modules["app_runner"] = _runner
        _spec.loader.exec_module(_runner)  # type: ignore[union-attr]

REPLY_PREFIX = "APP_WS_REPLY "
ENV_STREAM_PREFIX = "APP_WS_STREAM_"
ENV_STREAM_COUNT = "APP_WS_STREAM_COUNT"
ENV_TASK_API_KEY = "APP_TASK_API_KEY"
ENV_METRIC_API_KEY = "APP_METRIC_API_KEY"
RUN_FILENAME = "<run_code>"
METRIC_FILENAME = "<metric_code>"
DETAIL_MAX_CHARS = 4000
RUN_ERROR_MAX_CHARS = 300
RUN_CHILD_FLAG = "--run-child"
# Mirrors ``unified.dspy_engine.DSPY_STATE_KEY``; this file runs alone in the box.
DSPY_STATE_KEY = "__dspy_state__"
PREVIEW_MAX_CHARS = 2000
ARTIFACT_KIND_DSPY_PROGRAM = "dspy_program"
RUN_ENTRY_NAME = "run"
METRIC_ENTRY_NAME = "score"
# When the run code holds several modules, the one under this name is the program.
PROGRAM_NAME = "program"
_USER_FILENAMES = (RUN_FILENAME, METRIC_FILENAME)
FILES_DIR_PREFIX = "app_ws_files_"
# The run child's extra-files directory, so tracebacks name a helper file by
# its workspace path. Set only inside a run child.
_files_dir: str | None = None
_NUMBER_TYPES = (bool, int, float)
_PR_SET_DUMPABLE = 4
# The run child prints this on its real stdout right before it calls ``run``.
RUN_STARTED_MARKER = "APP_WS_RUN_STARTED"
OBSERVATION_VERSION = 1
OBSERVATION_HELPER_NAME = "observation"
SERVICE_COST_HELPER_NAME = "service_cost"
# What the run time covers: from the loaded run code calling ``run`` to its
# reply, model waits and retries included; interpreter start and loading the
# run code are ``setup_ms``, the metric is ``metric_ms``.
TIMING_BOUNDARY = "run_call"
COST_BASIS = "provider_usage"
# Only model calls made through ``llm`` are metered; other services the run
# code reaches are inside the run time but not in the cost.
COST_COVERAGE = "llm"
COST_METERED = "metered"
COST_UNAVAILABLE = "unavailable"
COST_NONE = "none"
REWARD_HELPER_NAME = "reward"
REWARD_FORMULA = "weighted_reference_utility_v1"
REWARD_COMPONENTS = ("accuracy", "speed", "cost")
_RELAY_ENV_NAMES = (_runner.ENV_BUDGET_RELAY_URL, "SKYNET_" + _runner.ENV_BUDGET_RELAY_URL[len("APP_") :])


class ShapeError(Exception):
    """The metric returned a value other than ``(score, side_info)``."""


class ServiceCostUnavailableError(Exception):
    """The current evaluation's service cost is not known, so it cannot be scored on."""


class RewardSpecError(ValueError):
    """The metric's ``REWARD_SPEC`` does not define a usable accuracy, speed and cost reward."""


def user_detail(exc: BaseException) -> str:
    """Render an exception with only the frames of the user's own code.

    Args:
        exc: The exception raised while running user code.

    Returns:
        A traceback-style text, at most :data:`DETAIL_MAX_CHARS` characters.
    """
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        if frame.filename in _USER_FILENAMES:
            frames.append(frame)
        elif _files_dir is not None and frame.filename.startswith(_files_dir + os.sep):
            frame.filename = frame.filename[len(_files_dir) + 1 :].replace(os.sep, "/")
            frames.append(frame)
    lines = ["Traceback (user code):\n", *traceback.format_list(frames)] if frames else []
    lines.extend(traceback.format_exception_only(type(exc), exc))
    text = "".join(lines).rstrip()
    return text if len(text) <= DETAIL_MAX_CHARS else text[-DETAIL_MAX_CHARS:]


def error_text(exc: BaseException) -> str:
    """Return ``"<Type>: <message>"``, the only error form an optimizer sees.

    Args:
        exc: The exception raised while running user code.

    Returns:
        The exception's type name and message.
    """
    return f"{type(exc).__name__}: {exc}"


def run_error_text(exc: BaseException) -> str:
    """Return the short form of a run-stage failure an optimizer may see.

    Candidate code chooses the exception message, so only its first line,
    capped at :data:`RUN_ERROR_MAX_CHARS`, ever leaves the run child.

    Args:
        exc: The exception raised by the run code or the candidate.

    Returns:
        ``"<Type>: <first line of the message>"``, capped.
    """
    lines = str(exc).strip().splitlines()
    text = f"{type(exc).__name__}: {lines[0] if lines else ''}".rstrip()
    return text[:RUN_ERROR_MAX_CHARS]


def preview(value: Any) -> str:
    """Return a short readable rendering of a run output.

    Args:
        value: Whatever the run code returned.

    Returns:
        JSON when the value serializes, else ``repr``, capped in length.
    """
    if hasattr(value, "toDict"):
        value = value.toDict()
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return text[:PREVIEW_MAX_CHARS]


def positional_capacity(fn: Callable[..., Any]) -> int:
    """Count how many positional arguments ``fn`` can take.

    Args:
        fn: A user function.

    Returns:
        The count, or a large number when it takes ``*args``.
    """
    params = inspect.signature(fn).parameters.values()
    if any(param.kind is param.VAR_POSITIONAL for param in params):
        return 1_000
    return sum(param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD) for param in params)


def _is_number(value: Any) -> bool:
    """Return True for a real number or boolean, never a numeric string."""
    return isinstance(value, _NUMBER_TYPES)


def normalize_metric(raw: Any) -> tuple[float, dict[str, Any]]:
    """Validate a metric's ``(score, side_info)`` return value and normalize it.

    Args:
        raw: What ``score`` returned.

    Returns:
        The score and side info, which always carries a ``feedback`` string.

    Raises:
        ShapeError: When the value is not ``(score, side_info)``.
    """
    if not isinstance(raw, (tuple, list)) or len(raw) != 2:
        raise ShapeError(f"expected (score, side_info), got {type(raw).__name__}")
    score, side_info = raw
    if not _is_number(score) or not isinstance(side_info, dict) or not isinstance(side_info.get("feedback"), str):
        raise ShapeError("expected a numeric score and a side_info dict with a feedback string")
    scores = side_info.get("scores")
    if scores is not None and (
        not isinstance(scores, dict) or not all(isinstance(k, str) and _is_number(v) for k, v in scores.items())
    ):
        raise ShapeError("side_info['scores'] must map names to numbers")
    plain = json.loads(json.dumps(side_info, default=_runner.side_info_json_default))
    return float(score), plain


def find_program(namespace: dict[str, Any], module_type: type) -> Any:
    """Find the DSPy module the run code builds at module level and calls in ``run``.

    Args:
        namespace: The executed run code's namespace.
        module_type: ``dspy.Module``.

    Returns:
        The single module-level ``dspy.Module`` instance, or the one named
        :data:`PROGRAM_NAME` when there are several.

    Raises:
        NameError: When the run code holds no module, or several and none is
            named :data:`PROGRAM_NAME`.
    """
    found: dict[int, tuple[str, Any]] = {}
    for name, value in namespace.items():
        if isinstance(value, module_type) and not name.startswith("__"):
            found.setdefault(id(value), (name, value))
    if len(found) == 1:
        return next(iter(found.values()))[1]
    named = namespace.get(PROGRAM_NAME)
    if isinstance(named, module_type):
        return named
    if not found:
        raise NameError(
            "the run code builds no dspy.Module: create it at module level "
            f"({PROGRAM_NAME} = MyProgram()) and call it inside run(artifact, input)"
        )
    names = ", ".join(sorted(name for name, _ in found.values()))
    raise NameError(f"the run code builds several dspy.Modules ({names}): name the one to optimize {PROGRAM_NAME}")


def apply_dspy_candidate(program: Any, candidate: Any) -> None:
    """Set a DSPy program's state and predictor instructions from a candidate.

    Args:
        program: The ``dspy.Module`` to update in place.
        candidate: ``{predictor name: instruction}``, optionally with the full
            program state under :data:`DSPY_STATE_KEY`; a plain string sets
            every predictor's instruction.
    """
    if isinstance(candidate, dict) and candidate.get(DSPY_STATE_KEY):
        # The compiled program's demos live here; instructions alone would
        # score a different program than the one that is saved.
        program.load_state(json.loads(candidate[DSPY_STATE_KEY]))
    for name, predictor in program.named_predictors():
        text = candidate if isinstance(candidate, str) else (candidate or {}).get(name)
        if text is not None:
            predictor.signature = predictor.signature.with_instructions(str(text))


def run_artifact(candidate: Any) -> Any:
    """Return the candidate as ``run`` receives it: without the serialized program state.

    Args:
        candidate: The version under optimization.

    Returns:
        The candidate, minus :data:`DSPY_STATE_KEY` when it is a mapping.
    """
    if isinstance(candidate, dict) and DSPY_STATE_KEY in candidate:
        return {key: value for key, value in candidate.items() if key != DSPY_STATE_KEY}
    return candidate


def _client(gateway: Any, key_env: str) -> Any:
    """Build the ``llm`` helper for one model, or ``None`` when no model is set.

    Args:
        gateway: The runner-shaped gateway section, if any.
        key_env: Environment variable holding this model's key when the edge does not inject it.

    Returns:
        A gateway client, or ``None``.
    """
    if not isinstance(gateway, dict):
        return None
    return _runner.GatewayClient(
        str(gateway["url"]),
        str(gateway["model"]),
        api_key=gateway.get("api_key") or os.environ.get(key_env),
        temperature=gateway.get("temperature"),
        max_tokens=gateway.get("max_tokens"),
        reasoning_effort=gateway.get("reasoning_effort"),
        timeout_seconds=float(gateway.get("timeout_seconds") or 120.0),
        protected=bool(gateway.get("protected")),
        declared_model=gateway.get("declared_model"),
        subscription=bool(gateway.get("subscription")),
        gateway_reasoning=bool(gateway.get("gateway_reasoning")),
    )


def _helpers(client: Any, extra: dict[str, Any] | None = None) -> Any:
    """Build the ``skynet`` module one piece of user code imports from.

    Args:
        client: The model this code's ``llm`` calls, or ``None``.
        extra: More helpers for this code only (the metric's observation helpers).

    Returns:
        The helper module.
    """
    return _runner.helper_module(
        {
            _runner.LLM_HELPER_NAME: client if client is not None else _runner.missing_llm,
            _runner.IMAGE_HELPER_NAME: _runner.Image,
            _runner.LOG_HELPER_NAME: _runner.log,
            **(extra or {}),
        }
    )


def _milliseconds(seconds: float) -> float:
    """Return a duration in milliseconds, rounded to a tenth."""
    return round(max(0.0, seconds) * 1000, 1)


def _count(value: Any) -> int | None:
    """Return a non-negative token count, or ``None`` when the value is not one."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def metered_cost(usage: Any) -> float | None:
    """Return the provider's full charge for one response, as the protected gateway bills it.

    Args:
        usage: The response's ``usage`` block.

    Returns:
        The charge in USD, or ``None`` when the response does not state all of it.
    """
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
        return None
    details = usage.get("cost_details")
    upstream = details.get("upstream_inference_cost") if isinstance(details, dict) else None
    byok = usage.get("is_byok")
    if byok is True:
        # A bring-your-own-key charge is the fee only; the provider bills the rest upstream.
        return float(cost) + float(upstream) if isinstance(upstream, (int, float)) and upstream >= 0 else None
    if byok is False or upstream in (None, 0):
        return float(cost)
    return None


def attempt_record(role: str, status: int, seconds: float, body: bytes) -> dict[str, Any]:
    """Describe one model attempt from what the gateway answered.

    Args:
        role: ``"task"`` or ``"metric"``.
        status: The HTTP status, or 0 when the gateway could not be reached.
        seconds: How long the attempt took.
        body: The response body.

    Returns:
        The attempt: status, success, time (the usage block's ``model_ms``
        when present), tokens and cost. A failed attempt
        without a usage block cost nothing (the provider refused it before
        anything billable existed); any other attempt without a stated cost
        has ``cost_usd`` ``None``.
    """
    try:
        payload = json.loads(body.decode("utf-8")) if body else None
    except ValueError:
        payload = None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    ok = 200 <= status < 300
    cost = metered_cost(usage)
    if cost is None and not ok and not isinstance(usage, dict):
        cost = 0.0
    # A CLI subscription call reports the model's own time; wall time would
    # also count the CLI's start-up, which is not the model's speed.
    model_ms = _count(usage.get("model_ms")) if isinstance(usage, dict) else None
    return {
        "role": role,
        "status": status,
        "ok": ok,
        "ms": _milliseconds(model_ms / 1000 if model_ms is not None else seconds),
        "prompt_tokens": _count(usage.get("prompt_tokens")) if isinstance(usage, dict) else None,
        "completion_tokens": _count(usage.get("completion_tokens")) if isinstance(usage, dict) else None,
        "total_tokens": _count(usage.get("total_tokens")) if isinstance(usage, dict) else None,
        "cost_usd": cost,
    }


def cost_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Total the metered cost of an evaluation's task-model attempts.

    Args:
        calls: The attempts, as :func:`attempt_record` describes them.

    Returns:
        ``unit``, ``status`` (``metered``, ``unavailable`` or ``none``), ``usd``
        (``None`` unless every attempt has a cost), ``basis``, ``coverage``,
        ``unpriced_calls`` and a ``reason`` when the cost is not known.
    """
    unpriced = sum(1 for call in calls if call.get("cost_usd") is None)
    summary: dict[str, Any] = {
        "unit": "USD",
        "basis": COST_BASIS,
        "coverage": COST_COVERAGE,
        "unpriced_calls": unpriced,
        "reason": None,
    }
    if not calls:
        return {**summary, "status": COST_NONE, "usd": 0.0}
    if unpriced:
        reason = (
            f"{unpriced} of {len(calls)} task-model call(s) returned no provider cost, "
            "so this response's service cost is not known"
        )
        return {**summary, "status": COST_UNAVAILABLE, "usd": None, "reason": reason}
    return {**summary, "status": COST_METERED, "usd": round(sum(float(call["cost_usd"]) for call in calls), 10)}


def build_observation(
    *,
    calls: list[dict[str, Any]],
    setup_seconds: float | None,
    task_seconds: float | None,
    conditions: dict[str, Any],
) -> dict[str, Any]:
    """Build the trusted observation of one evaluation's run stage.

    Args:
        calls: The task-model attempts the relay carried, in order.
        setup_seconds: Interpreter start and run-code load, measured here.
        task_seconds: The run call itself, measured here; ``None`` when ``run`` never started.
        conditions: The task model and its settings, the same for every version.

    Returns:
        ``version``, ``timing`` (milliseconds), ``calls``, call counts,
        ``cost`` (see :func:`cost_summary`) and ``conditions``.
    """
    return {
        "version": OBSERVATION_VERSION,
        "timing": {
            "unit": "ms",
            "boundary": TIMING_BOUNDARY,
            "task_ms": None if task_seconds is None else _milliseconds(task_seconds),
            "setup_ms": None if setup_seconds is None else _milliseconds(setup_seconds),
            "model_ms": round(sum(float(call.get("ms") or 0.0) for call in calls), 1),
        },
        "calls": [dict(call) for call in calls],
        "task_calls": len(calls),
        "failed_calls": sum(1 for call in calls if not call.get("ok")),
        "cost": cost_summary(calls),
        "conditions": dict(conditions),
    }


def _positive(value: Any, where: str) -> float:
    """Return ``value`` as a positive finite float or raise a :class:`RewardSpecError`."""
    if not _is_number(value) or isinstance(value, bool) or not 0 < float(value) < float("inf"):
        raise RewardSpecError(f"{where} must be a positive number")
    return float(value)


def reward_spec(raw: Any) -> dict[str, Any]:
    """Validate a metric's ``REWARD_SPEC`` and return it with weights that sum to 1.

    The spec is plain data, so the setup review and the run read the same
    fixed weights, units and reference scales.

    Args:
        raw: ``{"weights": {"accuracy", "speed", "cost"}, "accuracy": {...},
            "speed": {"reference": ms, ...}, "cost": {"reference": usd, ...}}``.
            ``accuracy.reference`` is optional: with it the metric passes an
            error (lower is better, such as a distance), without it a score
            from 0 to 1.

    Returns:
        The spec, each component a mapping and the weights normalized.

    Raises:
        RewardSpecError: When a weight, a reference or a component is missing or invalid.
    """
    if not isinstance(raw, dict):
        raise RewardSpecError("REWARD_SPEC must be a dict")
    weights = raw.get("weights")
    if not isinstance(weights, dict):
        raise RewardSpecError("REWARD_SPEC['weights'] must map accuracy, speed and cost to weights")
    parsed: dict[str, float] = {}
    for name in REWARD_COMPONENTS:
        value = weights.get(name)
        if not _is_number(value) or isinstance(value, bool) or not 0 <= float(value) < float("inf"):
            raise RewardSpecError(f"REWARD_SPEC['weights']['{name}'] must be a number of 0 or more")
        parsed[name] = float(value)
    total = sum(parsed.values())
    if total <= 0:
        raise RewardSpecError("REWARD_SPEC['weights'] must not all be 0")
    spec: dict[str, Any] = {"formula": REWARD_FORMULA, "weights": {k: v / total for k, v in parsed.items()}}
    for name in REWARD_COMPONENTS:
        component = raw.get(name) or {}
        if not isinstance(component, dict):
            raise RewardSpecError(f"REWARD_SPEC['{name}'] must be a dict")
        spec[name] = dict(component)
    for name in ("speed", "cost"):
        spec[name]["reference"] = _positive(spec[name].get("reference"), f"REWARD_SPEC['{name}']['reference']")
    if spec["accuracy"].get("reference") is not None:
        spec["accuracy"]["reference"] = _positive(spec["accuracy"]["reference"], "REWARD_SPEC['accuracy']['reference']")
    return spec


def reference_utility(value: float, reference: float) -> float:
    """Turn a cost-like measurement (0 or more, lower is better) into a utility in (0, 1].

    ``reference / (reference + value)``: 1 at 0, 0.5 at the reference, and
    strictly falling everywhere, so every saving counts on both sides of the
    reference and nothing plateaus or cuts off.
    """
    return reference / (reference + max(float(value), 0.0))


def blend_reward(
    spec: dict[str, Any], accuracy: float | None, observation: dict[str, Any] | None, accuracy_feedback: str = ""
) -> tuple[float, dict[str, Any]]:
    """Score one response on accuracy, speed and cost under a fixed reward spec.

    Each response is scored on its own measurements and the spec's fixed
    references only, so no other candidate can change its score. A missing
    accuracy value or an unavailable time or cost scores that component 0
    and says so in the feedback; it is never left out.

    Args:
        spec: A spec from :func:`reward_spec`.
        accuracy: The error (with ``accuracy.reference``) or the 0-1 score; ``None`` when the output has none.
        observation: The trusted observation of the run (see :func:`build_observation`).
        accuracy_feedback: The metric's own words on accuracy.

    Returns:
        ``(score, side_info)``: the weighted total; ``feedback`` (each
        component with its raw value), ``scores`` (one 0-1 utility per
        component) and ``reward`` (raw values,
        components, weights, contributions and what was unavailable).
    """
    weights = spec["weights"]
    unavailable: list[str] = []
    accuracy_reference = spec["accuracy"].get("reference")
    unit = spec["accuracy"].get("unit") or ""
    if accuracy is None or not _is_number(accuracy) or not math.isfinite(accuracy):
        # A NaN would make the whole total NaN (an output of "nan,nan" parses as a point).
        unavailable.append("accuracy")
        accuracy, accuracy_u, accuracy_text = None, 0.0, "no accuracy value (missing or unreadable output): scored 0"
    elif accuracy_reference is not None:
        accuracy_u = reference_utility(float(accuracy), accuracy_reference)
        accuracy_text = f"error {float(accuracy):g} {unit} (reference {accuracy_reference:g} {unit})".replace("  ", " ")
    else:
        accuracy_u = min(max(float(accuracy), 0.0), 1.0)
        accuracy_text = f"accuracy {accuracy_u:.3f}"
    timing = (observation or {}).get("timing") or {}
    time_ms = timing.get("task_ms")
    if not _is_number(time_ms):
        unavailable.append("speed")
        time_ms, speed_u, speed_text = None, 0.0, "response time unavailable: scored 0"
    else:
        speed_u = reference_utility(float(time_ms), spec["speed"]["reference"])
        speed_text = f"{float(time_ms):.0f} ms (reference {spec['speed']['reference']:g} ms)"
    cost = (observation or {}).get("cost") or {}
    cost_usd = cost.get("usd")
    if not _is_number(cost_usd):
        unavailable.append("cost")
        reason = cost.get("reason") or "no trusted cost"
        cost_usd, cost_u, cost_text = None, 0.0, f"service cost unavailable ({reason}): scored 0"
    else:
        cost_u = reference_utility(float(cost_usd), spec["cost"]["reference"])
        cost_text = f"${float(cost_usd):.6f} (reference ${spec['cost']['reference']:g})"
    components = {"accuracy": accuracy_u, "speed": speed_u, "cost": cost_u}
    contributions = {name: weights[name] * components[name] for name in REWARD_COMPONENTS}
    score = sum(contributions.values())
    if accuracy_feedback:
        accuracy_text = f"{accuracy_text}; {accuracy_feedback}"
    texts = {"accuracy": accuracy_text, "speed": speed_text, "cost": cost_text}
    feedback = "; ".join(f"{name} {components[name]:.3f}: {texts[name]}" for name in REWARD_COMPONENTS)
    side_info = {
        "feedback": f"reward {score:.3f}; {feedback}",
        "scores": dict(components),
        "reward": {
            "formula": spec["formula"],
            "raw": {"accuracy": accuracy, "time_ms": time_ms, "cost_usd": cost_usd},
            "components": components,
            "weights": dict(weights),
            "contributions": contributions,
            "unavailable": unavailable,
        },
    }
    return score, side_info


class TaskRelay:
    """Loopback endpoint in the metric process that carries the run child's model calls.

    The child is given only this address. Each attempt it sends is forwarded
    to the real gateway with the task key and recorded here, so retries count
    toward the evaluation that made them and the child cannot report its own
    usage. One relay serves one evaluation, which keeps concurrent
    evaluations apart.
    """

    def __init__(self, gateway: dict[str, Any], *, protected: bool) -> None:
        """Bind to a free loopback port without serving yet.

        Args:
            gateway: The task model's runner-shaped gateway section.
            protected: Whether the trusted parent owns dispatch, so an
                unreachable gateway is reported as pending usage.
        """
        upstream = next((value for value in (os.environ.get(name) for name in _RELAY_ENV_NAMES) if value), None)
        self.upstream = (upstream or str(gateway["url"])).rstrip("/")
        self.api_key = gateway.get("api_key") or os.environ.get(ENV_TASK_API_KEY)
        self.timeout_seconds = float(gateway.get("timeout_seconds") or 120.0)
        self.protected = protected
        self.calls: list[dict[str, Any]] = []
        # Concurrent evaluations share the box's loopback: only the child given
        # this secret path can spend through (and be billed by) this relay.
        self.prefix = "/" + secrets.token_urlsafe(16)
        self._lock = threading.Lock()
        self._ssl = _runner._ssl_context()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _RelayHandler)
        self._server.daemon_threads = True
        self._server.relay = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        """The base URL the run child posts chat completions to."""
        return f"http://127.0.0.1:{self._server.server_address[1]}{self.prefix}"

    def __enter__(self) -> TaskRelay:
        """Start serving."""
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()

    def forward(self, path: str, body: bytes) -> tuple[int, str, bytes]:
        """Send one attempt to the gateway and record it.

        Args:
            path: The request path under the gateway's base URL (``/chat/completions``).
            body: The child's JSON request body.

        Returns:
            The status, content type and body to give the child.
        """
        request = urllib.request.Request(
            self.upstream + path,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        started = time.perf_counter()
        content_type = "application/json"
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=self._ssl) as response:
                status, data = int(response.status), response.read()
                content_type = response.headers.get("Content-Type") or content_type
        except urllib.error.HTTPError as exc:
            status, data = int(exc.code), exc.read()
            content_type = exc.headers.get("Content-Type") or content_type
        except (urllib.error.URLError, OSError, ValueError) as exc:
            seconds = time.perf_counter() - started
            with self._lock:
                self.calls.append(attempt_record("task", 0, seconds, b""))
            # The child maps this code to the same pending-usage stop it raised before the relay existed.
            code = "provider_unavailable" if self.protected else "gateway_unreachable"
            message = json.dumps({"error": {"code": code, "message": f"the model gateway is unreachable: {exc}"}})
            return 502, "application/json", message.encode("utf-8")
        seconds = time.perf_counter() - started
        with self._lock:
            self.calls.append(attempt_record("task", status, seconds, data))
        return status, content_type, data

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a copy of the attempts recorded so far."""
        with self._lock:
            return [dict(call) for call in self.calls]


class _RelayHandler(BaseHTTPRequestHandler):
    """Hands every chat-completions POST to the owning :class:`TaskRelay`."""

    def do_POST(self) -> None:
        """Forward a request under the relay's secret path to the gateway; refuse any other."""
        relay: TaskRelay = self.server.relay  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        if not self.path.startswith(relay.prefix + "/"):
            # Also keeps the forwarded URL on the gateway's host: a path such as
            # ``@other.host/x`` appended to the base URL would change the host.
            status, content_type, data = 404, "application/json", b'{"error": {"code": "not_found"}}'
        else:
            status, content_type, data = relay.forward(self.path[len(relay.prefix) :], body)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep access-log lines out of the user's output."""


def _load(code: str, filename: str, module: Any) -> dict[str, Any]:
    """Execute user code with its own ``skynet`` module and return its namespace.

    Args:
        code: User-authored source, held in memory only.
        filename: Pseudo filename its frames carry.
        module: The ``skynet`` helper module for this code.

    Returns:
        The executed namespace.
    """
    # Registered in memory so the user's tracebacks show their own source lines.
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
    namespace: dict[str, Any] = dict(module.__dict__)
    sys.modules[_runner.HELPER_MODULE_NAME] = module
    exec(compile(code, filename, "exec", dont_inherit=True), namespace)
    return namespace


class _ProgramLM:
    """Factory for a ``dspy.BaseLM`` that sends every request through the task ``llm`` client."""

    @staticmethod
    def build(dspy: Any, client: Any, model: str) -> Any:
        """Return a DSPy LM bound to ``client``.

        Args:
            dspy: The imported ``dspy`` module.
            client: The task model's gateway client.
            model: Model id, for DSPy's history.

        Returns:
            A ``dspy.BaseLM`` instance.
        """

        class GatewayLM(dspy.BaseLM):
            """A DSPy LM whose requests go through the harness gateway client."""

            def forward(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> Any:
                """Complete one request and return an OpenAI-shaped response."""
                text = client(messages=messages or [{"role": "user", "content": str(prompt)}])
                message = type("Message", (), {"content": text})()
                choice = type("Choice", (), {"message": message})()
                return type("Response", (), {"choices": [choice], "usage": {}, "model": self.model})()

        return GatewayLM(model=model, cache=False)


def _plain_output(output: Any) -> tuple[Any, bool]:
    """Turn a run output into JSON-safe data the metric process can rebuild.

    Args:
        output: Whatever the run code returned.

    Returns:
        The JSON-safe value and whether it was a ``dspy.Prediction``.
    """
    is_prediction = hasattr(output, "toDict") and type(output).__name__ == "Prediction"
    value = output.toDict() if hasattr(output, "toDict") else output
    return json.loads(json.dumps(value, default=_runner.side_info_json_default)), is_prediction


class RunStage:
    """The run code, loaded in the run child with the task model as its ``llm``."""

    def __init__(self, message: dict[str, Any]) -> None:
        """Load the run code from the child's message.

        Args:
            message: ``{"run_code", "artifact_kind", "gateway"}``.

        Raises:
            BaseException: Whatever the run code raises while loading.
        """
        self.kind = str(message.get("artifact_kind") or "text")
        gateway = message.get("gateway")
        self.task_llm = _client(gateway, ENV_TASK_API_KEY)
        self.task_model = str((gateway or {}).get("model") or "task")
        self.program: Any = None
        self.program_lm: Any = None
        self.namespace = _load(str(message.get("run_code") or ""), RUN_FILENAME, _helpers(self.task_llm))
        if self.kind == ARTIFACT_KIND_DSPY_PROGRAM:
            # Imported here, before the run time starts, so a version is not timed on loading DSPy.
            importlib.import_module("dspy")

    def run(self, candidate: Any, inputs: dict[str, Any]) -> Any:
        """Produce the output for one case with the candidate applied.

        Args:
            candidate: The version under optimization.
            inputs: The case's input fields.

        Returns:
            What ``run(artifact, input)`` returned.
        """
        run = _entry(self.namespace, RUN_ENTRY_NAME)
        if self.kind != ARTIFACT_KIND_DSPY_PROGRAM:
            return run(candidate, inputs)
        dspy = importlib.import_module("dspy")
        if self.program is None:
            self.program = find_program(self.namespace, dspy.Module)
            if self.task_llm is not None:
                self.program_lm = _ProgramLM.build(dspy, self.task_llm, self.task_model)
        apply_dspy_candidate(self.program, candidate)
        if self.program_lm is None:
            return run(run_artifact(candidate), inputs)
        with dspy.context(lm=self.program_lm):
            return run(run_artifact(candidate), inputs)


def _safe_relative(path: str) -> bool:
    """Return whether ``path`` stays inside the directory it is joined to.

    Args:
        path: An extra file's workspace path; the API validated it already.

    Returns:
        True for a relative ``/``-separated path with no empty, ``.`` or ``..`` part.
    """
    parts = path.split("/")
    return bool(path) and not path.startswith("/") and "\\" not in path and all(p not in ("", ".", "..") for p in parts)


def write_files(files: dict[str, Any]) -> str:
    """Write the extra workspace files to a fresh directory.

    Args:
        files: Path to text of every extra file.

    Returns:
        The directory, to put at the front of ``sys.path``.

    Raises:
        ValueError: When a path would leave the directory.
    """
    root = tempfile.mkdtemp(prefix=FILES_DIR_PREFIX)
    for path, text in files.items():
        if not _safe_relative(str(path)):
            shutil.rmtree(root, ignore_errors=True)
            raise ValueError(f"unsafe workspace path: {path!r}")
        target = Path(root).joinpath(*str(path).split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(text), encoding="utf-8")
    return root


def run_child(message: dict[str, Any], on_started: Callable[[], None] | None = None) -> dict[str, Any]:
    """Run one case's run stage; the body of the ``--run-child`` process.

    Args:
        message: ``{"run_code", "files", "artifact_kind", "gateway", "candidate", "inputs"}``.
        on_started: Called once the run code is loaded, right before ``run``.

    Returns:
        ``{"output", "prediction", "output_preview", "usage"}``, or
        ``{"error", "detail", "usage"}``, or ``{"control", "usage"}``.
    """
    global _files_dir
    stage: RunStage | None = None
    reply: dict[str, Any]
    files = message.get("files") or {}
    try:
        if files:
            _files_dir = os.path.realpath(write_files(dict(files)))
            sys.path.insert(0, _files_dir)
        stage = RunStage(message)
        if on_started is not None:
            on_started()
        output = stage.run(message.get("candidate"), dict(message.get("inputs") or {}))
        plain, is_prediction = _plain_output(output)
        reply = {"output": plain, "prediction": is_prediction, "output_preview": preview(output)}
    except _runner.GatewayControl as exc:
        reply = {"control": {"code": exc.code, "message": str(exc)}}
    except BaseException as exc:  # user code is arbitrary — any failure is reported, not raised
        reply = {"error": run_error_text(exc), "detail": user_detail(exc)}
    finally:
        if _files_dir is not None:
            shutil.rmtree(_files_dir, ignore_errors=True)
    client = stage.task_llm if stage is not None else None
    if client is not None and client.control is not None:
        reply = {"control": client.control}
    reply["usage"] = list(client.usage) if client is not None else []
    return reply


def _child_env(relay_url: str | None = None) -> dict[str, str]:
    """Return the run child's environment: no message stream and no model key.

    Args:
        relay_url: The evaluation's :class:`TaskRelay`; it replaces any
            budget mailbox address, so every ``llm`` call goes through it.

    Returns:
        The environment.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(ENV_STREAM_PREFIX)
        and name not in (ENV_STREAM_COUNT, ENV_METRIC_API_KEY, ENV_TASK_API_KEY)
    }
    if relay_url is not None:
        for name in _RELAY_ENV_NAMES:
            if name in env:
                env[name] = relay_url
    return env


def _entry(namespace: dict[str, Any], name: str) -> Callable[..., Any]:
    """Return a required user function.

    Args:
        namespace: The executed code's namespace.
        name: The function's required name.

    Returns:
        The callable.

    Raises:
        NameError: When the code defines no such name.
        TypeError: When the name is not callable.
    """
    fn = namespace.get(name)
    if fn is None:
        raise NameError(f"the code does not define {name}()")
    if not callable(fn):
        raise TypeError(f"{name} is not a function")
    return fn


def _usage_entry(call: dict[str, Any]) -> dict[str, Any] | None:
    """Return one relayed attempt's token usage in the ledger's shape, or ``None`` without tokens."""
    if call.get("prompt_tokens") is None and call.get("completion_tokens") is None and call.get("total_tokens") is None:
        return None
    return {key: call.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}


class Workspace:
    """One loaded workspace: the metric here, the run code in a child per call."""

    def __init__(self, init: dict[str, Any]) -> None:
        """Load the metric and check the run code compiles.

        Args:
            init: The first protocol message.
        """
        self.kind = str(init.get("artifact_kind") or "text")
        gateways = init.get("gateways") or {}
        self.run_code = str(init.get("run_code") or "")
        self.files = dict(init.get("files") or {})
        self.task_gateway = gateways.get("task")
        self.metric_llm = _client(gateways.get("metric"), ENV_METRIC_API_KEY)
        self._observation: dict[str, Any] | None = None
        self.metric_module = _helpers(
            self.metric_llm,
            {
                OBSERVATION_HELPER_NAME: self.observation,
                SERVICE_COST_HELPER_NAME: self.service_cost,
                REWARD_HELPER_NAME: self.reward,
            },
        )
        self.load_error: dict[str, Any] | None = None
        self.metric_ns: dict[str, Any] = {}
        stage = "run"
        try:
            # Compiling never runs anything: a syntax error is reported once
            # here, everything else surfaces in the run child.
            compile(self.run_code, RUN_FILENAME, "exec", dont_inherit=True)
            stage = "metric"
            if self.kind == ARTIFACT_KIND_DSPY_PROGRAM:
                # Imported before any run child exists, so nothing a child
                # writes can stand in for it in this process.
                importlib.import_module("dspy")
            self.metric_ns = _load(str(init.get("metric_code") or ""), METRIC_FILENAME, self.metric_module)
        except _runner.GatewayControl as exc:
            self.load_error = {"control": {"code": exc.code, "message": str(exc)}}
        except BaseException as exc:  # user code is arbitrary — any failure is reported, not raised
            error = run_error_text(exc) if stage == "run" else error_text(exc)
            self.load_error = {"error": error, "stage": stage, "detail": user_detail(exc)}

    def _bind(self, module: Any) -> None:
        """Point ``import skynet`` at the helpers of the code about to run."""
        sys.modules[_runner.HELPER_MODULE_NAME] = module

    def observation(self) -> dict[str, Any]:
        """Return the current evaluation's trusted observation; the metric's ``observation()``.

        Returns:
            A copy of the observation (see :func:`build_observation`).

        Raises:
            RuntimeError: When called outside an evaluation.
        """
        if self._observation is None:
            raise RuntimeError("observation() is only available while score() runs")
        return json.loads(json.dumps(self._observation))

    def service_cost(self) -> float:
        """Return the current response's metered task-model cost in USD; the metric's ``service_cost()``.

        Returns:
            The cost; 0.0 when the run code made no model call.

        Raises:
            ServiceCostUnavailableError: When any call's cost is not known, so a
                cost-based metric cannot score a free-looking response.
        """
        cost = self.observation()["cost"]
        if cost["usd"] is None:
            raise ServiceCostUnavailableError(str(cost.get("reason") or "the service cost is not known"))
        return float(cost["usd"])

    def reward(self, spec: Any, accuracy: float | None, feedback: str = "") -> tuple[float, dict[str, Any]]:
        """Score the current response on accuracy, speed and cost; the metric's ``reward()``.

        Time and cost come from the trusted observation, never from the
        output, so the run code cannot make itself look fast or cheap.

        Args:
            spec: The metric's ``REWARD_SPEC`` (see :func:`reward_spec`).
            accuracy: The accuracy error or 0-1 score; ``None`` when the output has none.
            feedback: The metric's own words on accuracy.

        Returns:
            ``(score, side_info)`` ready for ``score`` to return (see :func:`blend_reward`).

        Raises:
            RewardSpecError: When ``spec`` is invalid.
            RuntimeError: When called outside an evaluation.
        """
        return blend_reward(reward_spec(spec), accuracy, self.observation(), feedback)

    def _conditions(self) -> dict[str, Any]:
        """Return the declared execution conditions every version runs under."""
        gateway = self.task_gateway if isinstance(self.task_gateway, dict) else {}
        return {
            "task_model": gateway.get("model"),
            "temperature": gateway.get("temperature"),
            "max_tokens": gateway.get("max_tokens"),
            "reasoning_effort": gateway.get("reasoning_effort"),
            "timeout_seconds": gateway.get("timeout_seconds"),
            "protected": bool(gateway.get("protected")),
            "artifact_kind": self.kind,
        }

    def _run(self, candidate: Any, inputs: dict[str, Any]) -> dict[str, Any]:
        """Run the run stage for one case in a fresh child interpreter, through a fresh relay.

        Args:
            candidate: The version under optimization.
            inputs: The case's input fields; the expected value never goes to the child.

        Returns:
            The child's reply (see :func:`run_child`), with ``usage`` and
            ``observation`` replaced by what this process measured.
        """
        if not isinstance(self.task_gateway, dict):
            return self._spawn(candidate, inputs, None, None)
        protected = bool(self.task_gateway.get("protected")) or any(os.environ.get(n) for n in _RELAY_ENV_NAMES)
        with TaskRelay(self.task_gateway, protected=protected) as relay:
            gateway = {**self.task_gateway, "url": relay.url, "api_key": None, "protected": protected}
            return self._spawn(candidate, inputs, gateway, relay)

    def _spawn(
        self, candidate: Any, inputs: dict[str, Any], gateway: dict[str, Any] | None, relay: TaskRelay | None
    ) -> dict[str, Any]:
        """Start the run child, time it from here and collect its reply.

        Args:
            candidate: The version under optimization.
            inputs: The case's input fields.
            gateway: The child's task gateway (the relay), if there is a task model.
            relay: The relay recording the child's model calls, if any.

        Returns:
            The child's reply with trusted ``usage`` and ``observation``.
        """
        message = {
            "run_code": self.run_code,
            "files": self.files,
            "artifact_kind": self.kind,
            "gateway": gateway,
            "candidate": candidate,
            "inputs": inputs,
        }
        data = json.dumps(message, default=_runner.side_info_json_default)
        spawned = time.perf_counter()
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), RUN_CHILD_FLAG],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=_child_env(relay.url if relay is not None else None),
            text=True,
        )
        started: float | None = None
        replied: float | None = None
        lines: list[str] = []
        if process.stdin is not None:
            with contextlib.suppress(BrokenPipeError):
                process.stdin.write(data)
                process.stdin.close()
        for line in process.stdout or ():
            now = time.perf_counter()
            # Only the first marker counts: candidate code runs after it and can only add later ones.
            if started is None and line.rstrip("\n") == RUN_STARTED_MARKER:
                started = now
            elif line.startswith(REPLY_PREFIX):
                replied = now
            lines.append(line)
        process.wait()
        ended = replied if replied is not None else time.perf_counter()
        calls = relay.snapshot() if relay is not None else []
        observation = build_observation(
            calls=calls,
            setup_seconds=(started if started is not None else ended) - spawned,
            task_seconds=None if started is None else ended - started,
            conditions=self._conditions(),
        )
        replies = parse_replies("".join(lines))
        if not replies:
            reply: dict[str, Any] = {
                "error": f"RuntimeError: the run code exited with status {process.returncode} without a result",
                "detail": f"the run process exited with status {process.returncode}",
            }
        else:
            reply = dict(replies[-1])
        reply["usage"] = [_usage_entry(call) for call in calls if _usage_entry(call) is not None]
        reply["observation"] = observation
        return reply

    def _metric(self, inputs: dict[str, Any], output: Any, expected: Any) -> Any:
        """Call ``score(input, output, expected=None)``.

        Args:
            inputs: The case's input fields.
            output: The run output.
            expected: The case's expected value, if any.

        Returns:
            The metric's raw return value.
        """
        fn = _entry(self.metric_ns, METRIC_ENTRY_NAME)
        return fn(inputs, output, expected) if positional_capacity(fn) >= 3 else fn(inputs, output)

    def _usage(self, task_usage: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Combine the run child's task usage with the metric's usage since the last reply.

        Args:
            task_usage: What the run child reported for the task model.

        Returns:
            ``{"task": [...], "metric": [...]}`` for the roles that have a model.
        """
        usage: dict[str, list[dict[str, Any]]] = {}
        if self.task_gateway is not None:
            usage["task"] = list(task_usage)
        if self.metric_llm is not None:
            usage["metric"] = list(self.metric_llm.usage)
            self.metric_llm.usage.clear()
        return usage

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        """Run one case through the run code (in a child) and the metric (here).

        Args:
            message: ``{"candidate": ..., "case": {...}}``.

        Returns:
            The reply document.
        """
        if self.load_error is not None:
            return {**self.load_error, "usage": self._usage([])}
        case = message.get("case") or {}
        inputs = dict(case.get("input") or {}) if isinstance(case, dict) else {}
        expected = case.get("expected") if isinstance(case, dict) else None
        ran = self._run(message.get("candidate"), inputs)
        task_usage = list(ran.get("usage") or [])
        observation = ran.get("observation")
        if isinstance(ran.get("control"), dict):
            return {"control": ran["control"], "usage": self._usage(task_usage)}
        if ran.get("error") is not None:
            return {
                "error": str(ran["error"])[:RUN_ERROR_MAX_CHARS],
                "stage": "run",
                "detail": str(ran.get("detail") or "")[-DETAIL_MAX_CHARS:],
                "usage": self._usage(task_usage),
                "observation": observation,
            }
        output = ran.get("output")
        if ran.get("prediction"):
            output = importlib.import_module("dspy").Prediction(**(output or {}))
        reply: dict[str, Any] = {"output_preview": ran.get("output_preview")}
        stage = "metric"
        metric_calls_before = len(self.metric_llm.usage) if self.metric_llm is not None else 0
        self._observation = observation
        metric_started = time.perf_counter()
        try:
            self._bind(self.metric_module)
            raw = self._metric(inputs, output, expected)
            stage = "shape"
            score, side_info = normalize_metric(raw)
            reply.update(score=score, side_info=side_info)
        except _runner.GatewayControl as exc:
            reply = {"control": {"code": exc.code, "message": str(exc)}}
        except ShapeError as exc:
            reply.update(error=str(exc), stage="shape", detail=str(exc))
        except BaseException as exc:  # user code is arbitrary — any failure is reported, not raised
            reply.update(error=error_text(exc), stage=stage, detail=user_detail(exc))
        finally:
            self._observation = None
        if self.metric_llm is not None and self.metric_llm.control is not None:
            reply = {"control": self.metric_llm.control}
        if isinstance(observation, dict) and "control" not in reply:
            metric_calls = len(self.metric_llm.usage) - metric_calls_before if self.metric_llm is not None else 0
            # Grading is kept apart from the response's own time and cost.
            observation["grading"] = {"ms": _milliseconds(time.perf_counter() - metric_started), "calls": metric_calls}
            reply["observation"] = observation
        reply["usage"] = self._usage(task_usage)
        return reply


def serve(lines: Iterable[str], out: TextIO) -> None:
    """Answer every protocol message in ``lines`` on ``out``.

    Args:
        lines: JSON lines; the first is the init message.
        out: Where prefixed replies are written.
    """
    workspace: Workspace | None = None
    for line in lines:
        if not line.strip():
            continue
        message = json.loads(line)
        if workspace is None:
            workspace = Workspace(message)
            reply: dict[str, Any] = workspace.load_error or {"ready": True}
        else:
            reply = workspace.handle(message)
        out.write(REPLY_PREFIX + json.dumps(reply, default=_runner.side_info_json_default) + "\n")
        out.flush()


def env_stream() -> list[str]:
    """Decode the message stream from the environment and remove it from there.

    Returns:
        The protocol lines.
    """
    count = int(os.environ.pop(ENV_STREAM_COUNT, "0") or 0)
    text = "".join(
        base64.b64decode(os.environ.pop(f"{ENV_STREAM_PREFIX}{index}", "")).decode("utf-8") for index in range(count)
    )
    return text.splitlines()


def encode_stream(messages: Iterable[dict[str, Any]], chunk_chars: int = 100_000) -> dict[str, str]:
    """Encode protocol messages into the environment ``--env`` reads.

    Args:
        messages: The init message, then the calls.
        chunk_chars: Largest single variable; one variable must stay under the kernel's per-string limit.

    Returns:
        The environment variables carrying the stream.
    """
    text = "".join(json.dumps(message, default=_runner.side_info_json_default) + "\n" for message in messages)
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    chunks = [encoded[start : start + chunk_chars] for start in range(0, len(encoded), chunk_chars)] or [""]
    return {ENV_STREAM_COUNT: str(len(chunks)), **{f"{ENV_STREAM_PREFIX}{i}": c for i, c in enumerate(chunks)}}


def parse_replies(stdout: str) -> list[dict[str, Any]]:
    """Return the harness replies found in a command's stdout.

    Args:
        stdout: The captured output.

    Returns:
        Each decoded reply, in order.
    """
    return [json.loads(line[len(REPLY_PREFIX) :]) for line in stdout.splitlines() if line.startswith(REPLY_PREFIX)]


def _forbid_inspection() -> None:
    """Make this process non-dumpable so a run child cannot read its memory or environment.

    The message stream (metric source, expected values) stays readable in
    ``/proc/<pid>/environ`` after it is popped from ``os.environ``; a
    non-dumpable process denies that and ``/proc/<pid>/mem`` to other
    processes of the same user. A no-op where ``prctl`` does not exist.
    """
    with contextlib.suppress(AttributeError, OSError):
        ctypes.CDLL(None).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)


def main(argv: list[str] | None = None) -> int:
    """Serve the stream from stdin, or from the environment with ``--env``.

    With ``--run-child`` this process is a run child instead: it reads one
    run message from stdin and writes one reply.

    Args:
        argv: Command-line arguments; ``sys.argv`` when unset.

    Returns:
        The process exit code.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout
    # User prints must never be mistaken for replies.
    sys.stdout = sys.stderr
    if RUN_CHILD_FLAG in args:

        def started() -> None:
            """Tell the metric process that ``run`` starts now."""
            out.write(RUN_STARTED_MARKER + "\n")
            out.flush()

        reply = run_child(json.loads(sys.stdin.read()), started)
        out.write(REPLY_PREFIX + json.dumps(reply, default=_runner.side_info_json_default) + "\n")
        out.flush()
        return 0
    _forbid_inspection()
    lines: Iterable[str] = env_stream() if "--env" in args else sys.stdin
    serve(lines, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

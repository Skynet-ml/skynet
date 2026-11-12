"""Derive the structural facts a run-outcome telemetry event carries.

The worker emits ``run_completed`` / ``run_failed`` once per run. This module
turns the stored black-box result, the stored payload and the terminating exception into flat, PII-free properties, so the
metric SQL reads plain columns instead of parsing every result shape.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from ..billing.budgets import BudgetError
from ..billing.operation_pricing import UnpricedOperationError
from ..billing.remote_evaluator import RemoteEvaluatorTransportError
from ..billing.runtime import UsagePendingError
from ..exceptions import InfrastructureInterruptionError
from ..models.results import result_scores

FAILURE_STAGE_PREFLIGHT = "preflight"
FAILURE_STAGE_SANDBOX = "sandbox"
FAILURE_STAGE_OPTIMIZE = "optimize"
FAILURE_STAGE_EVALUATE = "evaluate"
FAILURE_STAGE_BILLING = "billing"
FAILURE_STAGE_UNKNOWN = "unknown"
FAILURE_STAGES = frozenset(
    {
        FAILURE_STAGE_PREFLIGHT,
        FAILURE_STAGE_SANDBOX,
        FAILURE_STAGE_OPTIMIZE,
        FAILURE_STAGE_EVALUATE,
        FAILURE_STAGE_BILLING,
        FAILURE_STAGE_UNKNOWN,
    }
)

_BILLING_ERRORS = (BudgetError, UsagePendingError, UnpricedOperationError)
# A child process reports its failure only as a class name, so these name
# fragments stand in for the isinstance checks above.
_BILLING_NAME_HINTS = ("Budget", "UsagePending", "Unpriced", "ProviderFailed")
_EVALUATE_NAME_HINTS = ("Scorer", "Evaluator", "RepoScore", "AgentHarness")
_SANDBOX_NAME_HINTS = ("Sandbox", "InfrastructureInterruption")


def _number(value: Any) -> float | None:
    """Coerce a stored score to ``float``, rejecting booleans and non-numbers.

    Args:
        value: Raw score read from a stored result.

    Returns:
        The score as a float, or ``None`` when it is not a real number.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _split_test_count(split: Any) -> int | None:
    """Return a ``split_counts`` mapping's test-row count.

    Args:
        split: The stored ``split_counts`` value, if any.

    Returns:
        The non-negative test count, or ``None`` when it is not recorded.
    """
    if not isinstance(split, Mapping):
        return None
    test = split.get("test")
    if isinstance(test, bool) or not isinstance(test, int) or test < 0:
        return None
    return test


def score_facts(result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read baseline, optimized, lift and held-out facts from a stored result.

    A unified result is held out exactly when its report says so. A result
    without a split is never held out.

    Args:
        result: The stored ``result`` mapping of a finished run.

    Returns:
        ``baseline``, ``optimized``, ``lift`` (``None`` unless both scores
        exist), ``held_out`` and ``held_out_rows``.
    """
    if not isinstance(result, Mapping):
        return {"baseline": None, "optimized": None, "lift": None, "held_out": False, "held_out_rows": None}
    baseline_raw, optimized_raw = result_scores(dict(result))
    baseline, optimized = _number(baseline_raw), _number(optimized_raw)
    lift = optimized - baseline if baseline is not None and optimized is not None else None
    split = result.get("split_counts")
    return {
        "baseline": baseline,
        "optimized": optimized,
        "lift": lift,
        "held_out": isinstance(split, Mapping) and split.get("held_out") is True,
        "held_out_rows": _split_test_count(split),
    }


def _leaves(value: Any) -> Iterable[str]:
    """Yield every scalar inside a row as canonical text.

    Args:
        value: A row, case, or nested value within one.

    Returns:
        The row's scalar leaves, each serialized the same way whatever
        container held it.
    """
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _leaves(item)
    elif value is not None:
        yield json.dumps(value, ensure_ascii=False, sort_keys=True)


def row_fingerprint(row: Any) -> str:
    """Fingerprint one row by its values, ignoring keys and nesting.

    A stored payload holds a sample as flat DSPy rows (older runs), as
    black-box cases or as unified ``{"input", "expected"}`` cases; all three
    carry the same values, so a value-only fingerprint matches a sample
    whichever shape staged it.

    Args:
        row: One dataset row or case.

    Returns:
        A hex digest of the row's sorted scalar values.
    """
    joined = "\x1f".join(sorted(_leaves(row)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def payload_rows(payload: Mapping[str, Any] | None) -> list[Any]:
    """Return the dataset rows a stored payload carries inline.

    Args:
        payload: The stored job payload.

    Returns:
        Black-box ``cases`` or unified ``data.cases``; empty when the
        payload holds none.
    """
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    for rows in (
        payload.get("cases"),
        data.get("cases") if isinstance(data, Mapping) else None,
    ):
        if isinstance(rows, list) and rows:
            return rows
    return []


def is_sample_dataset(rows: list[Any], samples: Mapping[str, Mapping[str, Any]]) -> bool:
    """Decide whether a job's rows all come from one bundled sample dataset.

    Nothing on a job records sample provenance, so the rows themselves are
    matched: every row must equal (by value) a row of the same sample.

    Args:
        rows: The job's dataset rows or cases.
        samples: Bundled samples keyed by id, each with a ``rows`` list.

    Returns:
        ``True`` when ``rows`` is non-empty and every row is in one sample.
    """
    if not rows:
        return False
    fingerprints = {row_fingerprint(row) for row in rows}
    for sample in samples.values():
        sample_rows = sample.get("rows") if isinstance(sample, Mapping) else None
        if isinstance(sample_rows, list) and fingerprints <= {row_fingerprint(row) for row in sample_rows}:
            return True
    return False


def completed_properties(
    result: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
    samples: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the extra ``run_completed`` properties for one finished run.

    Args:
        result: The stored result mapping.
        payload: The stored job payload holding the dataset rows.
        samples: Bundled sample datasets (``core.i18n.SAMPLE_DATASETS``).

    Returns:
        Score and held-out facts plus ``from_sample``.
    """
    return {**score_facts(result), "from_sample": is_sample_dataset(payload_rows(payload), samples)}


def error_class(exc: BaseException) -> str:
    """Return the class name that best describes a run failure.

    A failure inside the optimizer process reaches the worker re-raised as a
    generic error; its original class, when known, is on ``remote_error_class``.

    Args:
        exc: The exception that ended the run.

    Returns:
        The exception class name; never the message.
    """
    remote = getattr(exc, "remote_error_class", None)
    return remote if isinstance(remote, str) and remote else type(exc).__name__


def failure_stage(exc: BaseException, phase: str | None) -> str:
    """Classify where a run failed.

    The exception's type wins when it names a stage (billing, evaluator
    transport, sandbox/infrastructure); otherwise the worker's phase at the
    time of the failure decides.

    Args:
        exc: The exception that ended the run.
        phase: The worker's phase when it failed (one of :data:`FAILURE_STAGES`).

    Returns:
        One of :data:`FAILURE_STAGES`.
    """
    if isinstance(exc, _BILLING_ERRORS):
        return FAILURE_STAGE_BILLING
    if isinstance(exc, RemoteEvaluatorTransportError):
        return FAILURE_STAGE_EVALUATE
    if isinstance(exc, InfrastructureInterruptionError):
        return FAILURE_STAGE_SANDBOX
    name = error_class(exc)
    if any(hint in name for hint in _BILLING_NAME_HINTS):
        return FAILURE_STAGE_BILLING
    if any(hint in name for hint in _EVALUATE_NAME_HINTS):
        return FAILURE_STAGE_EVALUATE
    if any(hint in name for hint in _SANDBOX_NAME_HINTS):
        return FAILURE_STAGE_SANDBOX
    return phase if phase in FAILURE_STAGES else FAILURE_STAGE_UNKNOWN


def failed_properties(exc: BaseException | None, phase: str | None) -> dict[str, Any]:
    """Build the extra ``run_failed`` properties for one failed run.

    Args:
        exc: The exception that ended the run, or ``None`` when the run ended
            failed without one (an optimizer-reported terminal failure).
        phase: The worker's phase when it failed.

    Returns:
        ``failure_stage`` and ``error_class``; no error text.
    """
    if exc is None:
        return {
            "failure_stage": phase if phase in FAILURE_STAGES else FAILURE_STAGE_UNKNOWN,
            "error_class": None,
        }
    return {"failure_stage": failure_stage(exc, phase), "error_class": error_class(exc)}

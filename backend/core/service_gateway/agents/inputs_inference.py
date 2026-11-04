"""LLM inference for the submission wizard's step-2 inputs. [INTERNAL]

Given the task from step 1 and a sample of the user's data, one
non-streamed prediction decides whether the run optimizes against a single
input or many input/output pairs, and which columns are the inputs and the
expected output. Everything the model writes is validated against the real
columns here; the held-out split and its size are computed deterministically,
never by the model.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import dspy

from ...api.model_catalog import agent_model_id
from ...config import settings
from ...i18n import t_locale
from ...models import (
    MIN_PAIRS_FOR_HELD_OUT,
    DataMode,
    InputsInferenceRequest,
    InputsInferenceResponse,
    ModelConfig,
)
from ...models.common import SplitFractions
from .code import _build_agent_lm, _reply_language
from .code_interview import _parse_json
from .conduct import with_conduct

logger = logging.getLogger(__name__)

INFERENCE_MAX_TOKENS = 4000
"""Completion budget, reasoning included; the answer is a few short JSON fields."""
MAX_SAMPLE_CHARS = 6000
"""Cap on the JSON of the sample rows fed to the model."""
MAX_VERSION_CHARS = 4000
"""Cap on the starting version fed to the model."""
WARNING_TOO_FEW_PAIRS = "inputs.too_few_pairs_for_held_out"


class InputsInferenceSig(dspy.Signature):
    __doc__ = with_conduct("""Decide what the optimization run is evaluated on, from the task and the user's data.

    The run improves an artifact (``artifact_kind``) toward ``objective``.
    For ``repo`` the artifact is a GitHub repository: each input is one
    case its scorer runs against the repository, such as a page or a
    prompt, and the expected output is what that case should produce.
    Decide ``mode``:
    - ``single``: the artifact is judged on ONE input (e.g. a single
      document to rewrite, one problem to solve), or there is no table.
    - ``pairs``: the data has many rows, each an input and usually an
      expected output, and the artifact must do well across them.
    Prefer ``pairs`` when the data has two or more rows that are examples
    of the same task.

    Pick ``input_fields_json``: the columns the artifact reads as its
    input, only names from ``columns``. Pick ``expected_field``: the one
    column holding the reference output (the answer, label, target), or
    empty when there is none; it is never an input field. For ``single``
    write ``single_input_json``: the one input as a JSON object keyed by
    the input fields (from the most representative sample row, or built
    from the objective and note when there are no columns).
    ``rationale`` is one or two short sentences in ``reply_language``
    explaining the choice to a non-technical user.
    """)

    objective: str = dspy.InputField(desc="What the run must improve.")
    background: str = dspy.InputField(desc="Extra task context; may be empty.")
    artifact_kind: str = dspy.InputField(desc="dspy_program, or repo for a GitHub repository.")
    starting_version: str = dspy.InputField(desc="The artifact's starting version as JSON; may be empty.")
    columns: list[str] = dspy.InputField(desc="Column names of the user's data; may be empty.")
    sample_rows: str = dspy.InputField(desc="JSON array of up to five sample rows.")
    row_count: int = dspy.InputField(desc="Total number of rows in the data.")
    user_note: str = dspy.InputField(desc="Anything the user said about the data; may be empty.")
    reply_language: str = dspy.InputField(desc="Language the rationale is written in.")
    mode: str = dspy.OutputField(desc="'single' or 'pairs'.")
    input_fields_json: str = dspy.OutputField(desc="JSON array of input column names.")
    expected_field: str = dspy.OutputField(desc="The expected-output column name, or empty.")
    single_input_json: str = dspy.OutputField(desc="For single mode, the input as a JSON object; else {}.")
    rationale: str = dspy.OutputField(desc="One or two short sentences explaining the choice.")


def suggested_split(mode: DataMode, row_count: int) -> SplitFractions:
    """Return the split fractions the wizard proposes.

    Below :data:`MIN_PAIRS_FOR_HELD_OUT` pairs the test share folds into
    train, matching the server-side split which holds nothing out then.

    Args:
        mode: The data mode.
        row_count: Number of cases.

    Returns:
        The proposed fractions.
    """
    default = SplitFractions()
    if mode == "pairs" and row_count < MIN_PAIRS_FOR_HELD_OUT:
        return SplitFractions(train=round(default.train + default.test, 6), val=default.val, test=0.0)
    return default


def held_out_count(mode: DataMode, row_count: int, split: SplitFractions) -> int:
    """Count the cases the split holds out for the final report.

    Args:
        mode: The data mode; single mode holds nothing out.
        row_count: Number of cases.
        split: The split fractions.

    Returns:
        Number of test cases: at least one when the test share is positive
        and there are enough pairs, never all of them.
    """
    if mode != "pairs" or row_count < MIN_PAIRS_FOR_HELD_OUT or split.test <= 0:
        return 0
    return min(max(1, round(row_count * split.test)), row_count - 1)


def _fallback_mode(req: InputsInferenceRequest) -> DataMode:
    """Pick the mode without the model: pairs whenever there are two or more rows.

    Args:
        req: The inference request.

    Returns:
        The data mode.
    """
    return "pairs" if req.row_count >= 2 else "single"


def _validate_fields(raw_fields: Any, raw_expected: Any, columns: list[str]) -> tuple[list[str], str | None]:
    """Keep only real columns, with the expected field never an input.

    With no columns (no table uploaded) the model's names are kept as they
    are, since they then name the keys of a hand-written input.

    Args:
        raw_fields: Parsed ``input_fields_json``.
        raw_expected: Raw ``expected_field``.
        columns: The data's column names.

    Returns:
        ``(input_fields, expected_field)``.
    """
    known = set(columns)
    expected = str(raw_expected or "").strip() or None
    if columns and expected not in known:
        expected = None
    fields: list[str] = []
    if isinstance(raw_fields, list):
        for item in raw_fields:
            name = str(item).strip()
            if name and name != expected and name not in fields and (not columns or name in known):
                fields.append(name)
    if not fields:
        fields = [c for c in columns if c != expected]
    return fields, expected


def _single_input(raw: Any, fields: list[str], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Build the one input of a single-mode run.

    Args:
        raw: Parsed ``single_input_json``.
        fields: The validated input fields.
        rows: The sample rows.

    Returns:
        The input dict, or ``None`` when there is nothing to build it from.
    """
    if isinstance(raw, dict) and raw:
        picked = {k: v for k, v in raw.items() if not fields or k in fields}
        if picked:
            return picked
    if rows:
        return {k: rows[0].get(k) for k in fields} if fields else dict(rows[0])
    return None


def _predict(req: InputsInferenceRequest, usage_sink: list | None, model_config: ModelConfig | None = None) -> Any:
    """Run the inference predictor once.

    Args:
        req: The inference request.
        usage_sink: Optional list the built LM is appended to, for metering.
        model_config: Optional resolved config for ``req.model`` (the Mac
            app's BYOK route).

    Returns:
        The raw prediction.
    """
    lm = _build_agent_lm(req.model, max_tokens=INFERENCE_MAX_TOKENS, model_config=model_config)
    if usage_sink is not None:
        usage_sink.append(lm)
    version = "" if req.starting_version is None else json.dumps(req.starting_version, ensure_ascii=False)
    with dspy.context(lm=lm):
        return dspy.Predict(InputsInferenceSig)(
            objective=req.objective,
            background=req.background,
            artifact_kind=req.artifact_kind,
            starting_version=version[:MAX_VERSION_CHARS],
            columns=list(req.columns),
            sample_rows=json.dumps(req.sample_rows, ensure_ascii=False, default=str)[:MAX_SAMPLE_CHARS],
            row_count=req.row_count,
            user_note=req.user_note,
            reply_language=_reply_language(req.locale),
        )


def infer_inputs(
    req: InputsInferenceRequest,
    *,
    usage_sink: list | None = None,
    model_config: ModelConfig | None = None,
) -> InputsInferenceResponse:
    """Decide single vs pairs and the input/expected columns for step 2.

    A failed model call falls back to a column-only guess (pairs when there
    are two or more rows, every column but none expected), so the wizard is
    never left without a proposal.

    Args:
        req: The task and the data sample.
        usage_sink: Optional list the built LM is appended to, for metering.
        model_config: Optional resolved config for ``req.model`` (the Mac
            app's BYOK route).

    Returns:
        The validated proposal, with a deterministic split and held-out count.
    """
    try:
        pred: Any = _predict(req, usage_sink, model_config)
    except Exception:
        logger.warning("inputs inference failed; falling back to the column guess", exc_info=True)
        pred = None
    raw_mode = str(getattr(pred, "mode", "") or "").strip().lower()
    mode: DataMode = raw_mode if raw_mode in ("single", "pairs") else _fallback_mode(req)  # type: ignore[assignment]
    if mode == "pairs" and req.row_count < 2:
        mode = "single"
    fields, expected = _validate_fields(
        _parse_json(str(getattr(pred, "input_fields_json", "") or ""), []),
        getattr(pred, "expected_field", ""),
        list(req.columns),
    )
    single = None
    if mode == "single":
        single = _single_input(
            _parse_json(str(getattr(pred, "single_input_json", "") or ""), {}), fields, req.sample_rows
        )
    split = suggested_split(mode, req.row_count)
    warnings: list[str] = []
    if mode == "pairs" and req.row_count < MIN_PAIRS_FOR_HELD_OUT:
        warnings.append(t_locale(req.locale, WARNING_TOO_FEW_PAIRS, minimum=MIN_PAIRS_FOR_HELD_OUT))
    return InputsInferenceResponse(
        mode=mode,
        input_fields=fields,
        expected_field=expected,
        single_input=single,
        rationale=str(getattr(pred, "rationale", "") or "").strip(),
        suggested_split=split,
        held_out_count=held_out_count(mode, req.row_count, split),
        warnings=warnings,
        model=req.model or agent_model_id(settings.code_agent_model),
    )

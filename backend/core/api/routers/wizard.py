"""Routes for agent-driven wizard-state mutation. [INTERNAL]

``POST /wizard/update`` is the single endpoint the generalist agent uses
to edit any subset of wizard fields in one call. Each supplied field is
validated against a narrow whitelist and echoed back in a
``wizard_state`` patch that the frontend mirrors into the live submit
wizard. The keys follow the wizard's four steps: task and artifact,
inputs, optimizer (with models and budget), and the metric/code workspace.

Hidden from the public Scalar reference (not in ``_SCALAR_PUBLIC_PATHS``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from ...models import DataMode, OptimizerChoice, OptimizerTarget, optimizers_for
from ...models.blackbox import BlackboxCandidate
from ..errors import DomainError


class WizardUpdateRequest(BaseModel):
    """Partial update for any subset of editable wizard fields.

    Every field is optional. Only supplied fields are validated and
    echoed into the ``wizard_state`` patch returned in the response.
    Fields not supplied are left untouched on the frontend.
    """

    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())

    job_name: str | None = Field(default=None, max_length=200)
    job_description: str | None = Field(default=None, max_length=500)
    is_private: bool | None = Field(
        default=None,
        description="When true (the default for new runs), the run is excluded from public Explore.",
    )

    task_objective: str | None = Field(
        default=None,
        max_length=8000,
        description="Task step: what a better version means, in the user's words.",
    )
    task_background: str | None = Field(
        default=None,
        max_length=20_000,
        description="Task step: context the optimizer should know about the task.",
    )
    artifact_kind: OptimizerTarget | None = Field(
        default=None,
        description="Task step: what is optimized. Every run optimizes a GitHub repository, so send 'repo'.",
    )
    starting_version: BlackboxCandidate | None = Field(
        default=None,
        description=(
            "Task step: the starting version, a string or a map of named parts to strings. A 'repo' run has none."
        ),
    )
    repository: str | None = Field(
        default=None,
        max_length=201,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        description="Task step: the GitHub repository a 'repo' run optimizes, as owner/name.",
    )

    data_mode: DataMode | None = Field(
        default=None,
        description="Inputs step: 'single' for one input, 'pairs' for input/output pairs with a held-out split.",
    )
    staged_dataset_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Inputs step: id of a staged dataset that holds the input/output pairs.",
    )
    split_fractions: dict[str, float] | None = Field(
        default=None,
        description="Inputs step: train/val/test fractions. Must sum to 1.0 and be non-negative.",
    )
    shuffle: bool | None = Field(default=None)
    seed: int | None = Field(default=None, description="Shuffle/split seed.")

    optimizer_choice: OptimizerChoice | None = Field(
        default=None,
        description=(
            "Optimizer step: 'auto' or one repository engine id (gepa, best_of_n, autoresearch, "
            "meta_harness, autosaddler, shinka_evolve). Other ids are rejected (see GET /optimizers)."
        ),
    )
    primary_model: dict[str, Any] | None = Field(
        default=None,
        alias="model_config",
        description="Optimizer step: task model config dict (name required, other fields optional).",
    )
    reflection_model: dict[str, Any] | None = Field(
        default=None,
        alias="reflection_model_config",
        description="Optimizer step: reflection model config dict, the model that proposes new versions.",
    )
    spending_limit_usd: float | None = Field(
        default=None,
        gt=0,
        le=100_000,
        description="Total spending limit for the run in US dollars. Send null explicitly to remove the limit.",
    )

    run_code: str | None = Field(
        default=None,
        description="Metric step: the whole run.py, which runs the repository checkout on one input. Replaces the file.",
    )
    metric_code: str | None = Field(
        default=None,
        description="Metric step: the whole metric.py, which scores one output. Replaces the file.",
    )


class WizardUpdateResponse(BaseModel):
    """Envelope returning the validated ``wizard_state`` patch."""

    wizard_state: dict[str, Any]


def validate_model_config_dict(v: Any, field_name: str) -> dict[str, Any]:
    """Light-weight validation of a ``ModelConfig``-shaped dict.

    Allows partial dicts (``{"name": "openai/gpt-4o"}``) for progressive
    configuration.

    Args:
        v: Candidate model-config dict from the request.
        field_name: Field label used in error messages.

    Returns:
        A cleaned dict the frontend can merge into its local
        ``ModelConfig`` state via object spread.

    Raises:
        DomainError: 422 with a wizard-prefixed key when the input is not
            a JSON object or any field fails type validation.
    """
    if not isinstance(v, dict):
        raise DomainError("wizard.model_not_json_object", status=422, field=field_name)
    out: dict[str, Any] = {}
    name = v.get("name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            raise DomainError("wizard.model_name_non_empty_string", status=422, field=field_name)
        cleaned = name.strip()
        # dspy.LM rejects un-prefixed ids; reject early with an actionable
        # error so the agent self-corrects by re-reading list_models_for_agent.
        if "/" not in cleaned:
            raise DomainError(
                "wizard.model_name_missing_prefix",
                status=422,
                field=field_name,
                name=cleaned,
            )
        out["name"] = cleaned
    base_url = v.get("base_url")
    if base_url is not None:
        if not isinstance(base_url, str):
            raise DomainError("wizard.model_base_url_not_string", status=422, field=field_name)
        out["base_url"] = base_url
    temperature = v.get("temperature")
    if temperature is not None:
        if not isinstance(temperature, (int, float)):
            raise DomainError(
                "wizard.model_number_required",
                status=422,
                field=field_name,
                key="temperature",
            )
        out["temperature"] = float(temperature)
    max_tokens = v.get("max_tokens")
    if max_tokens is not None:
        if not isinstance(max_tokens, int):
            raise DomainError("wizard.model_max_tokens_int", status=422, field=field_name)
        out["max_tokens"] = max_tokens
    extra = v.get("extra")
    if extra is not None:
        if not isinstance(extra, dict):
            raise DomainError("wizard.model_extra_not_object", status=422, field=field_name)
        out["extra"] = extra
    return out


def create_wizard_router() -> APIRouter:
    """Build the wizard-state router.

    Returns:
        A FastAPI ``APIRouter`` exposing ``POST /wizard/update``.
    """
    router = APIRouter()

    @router.post(
        "/wizard/update",
        response_model=WizardUpdateResponse,
        status_code=200,
        operation_id="update_wizard_state",
        summary="Patch any subset of editable wizard fields",
        tags=["agent"],
    )
    def update_wizard_state(req: WizardUpdateRequest) -> WizardUpdateResponse:
        """Validate and echo any subset of wizard fields as a ``wizard_state`` patch.

        Covers every editable field in the submit wizard: job metadata, the
        task and artifact, the inputs and split, the optimizer with its
        models and spending limit, and the workspace code. Supply only the
        fields you want to change — the rest are left untouched on the
        frontend. Workspace code (``run_code``, ``metric_code``) replaces
        the whole file; the wizard's setup check runs it before Run.

        BATCHING — make ONE call per turn covering every field you intend
        to set together (``task_objective`` + ``artifact_kind`` +
        ``repository`` + …), not one call per field. Sequential
        same-turn calls render as duplicate spinner pills in the UI and
        waste turns on identical round-trips.

        Args:
            req: The partial-update body (every field optional).

        Returns:
            A ``WizardUpdateResponse`` carrying the validated patch dict.

        Raises:
            DomainError: 422 if any supplied field fails validation
                (blank text, bad split sum, malformed model dict, an
                optimizer that does not fit the artifact and data mode,
                blank workspace code, etc.).
        """
        supplied = req.model_dump(by_alias=True, exclude_unset=True)
        patch: dict[str, Any] = {}

        # Code keeps its own whitespace: indentation is part of it.
        for code_field in ("run_code", "metric_code"):
            code = supplied.get(code_field)
            if code is not None:
                if not code.strip():
                    raise DomainError("wizard.field_non_empty_string", status=422, field=code_field)
                patch[code_field] = code

        for key in ("job_name", "job_description", "task_objective"):
            if key in supplied and supplied[key] is not None:
                val = supplied[key]
                # The description is optional, so an empty one clears it; agents
                # sent "" for it and lost the whole write to a 422.
                if key == "job_description" and isinstance(val, str) and not val.strip():
                    patch[key] = ""
                    continue
                if not isinstance(val, str) or not val.strip():
                    raise DomainError("wizard.field_non_empty_string", status=422, field=key)
                patch[key] = val.strip()

        if supplied.get("task_background") is not None:
            patch["task_background"] = supplied["task_background"].strip()

        for literal_key in ("artifact_kind", "data_mode", "optimizer_choice", "staged_dataset_id", "repository"):
            if supplied.get(literal_key) is not None:
                patch[literal_key] = supplied[literal_key]

        if supplied.get("starting_version") is not None:
            patch["starting_version"] = supplied["starting_version"]

        # Skynet is repo-only (SKY-316), so a choice sent alone is checked
        # against the repository engines; the repo list ignores the data mode.
        if req.optimizer_choice is not None:
            allowed = optimizers_for(req.artifact_kind or "repo", req.data_mode or "single")
            if req.optimizer_choice not in allowed:
                raise DomainError(
                    "submit.unified.optimizer_not_allowed",
                    status=422,
                    optimizer=req.optimizer_choice,
                    allowed=", ".join(allowed),
                )

        if "model_config" in supplied and supplied["model_config"] is not None:
            cleaned = validate_model_config_dict(supplied["model_config"], "model_config")
            patch["model_config"] = cleaned
            if cleaned.get("name"):
                patch["model_configured"] = True

        if "reflection_model_config" in supplied and supplied["reflection_model_config"] is not None:
            patch["reflection_model_config"] = validate_model_config_dict(
                supplied["reflection_model_config"], "reflection_model_config"
            )

        for bool_key in ("shuffle", "is_private"):
            if bool_key in supplied and supplied[bool_key] is not None:
                patch[bool_key] = bool(supplied[bool_key])

        if "split_fractions" in supplied and supplied["split_fractions"] is not None:
            sf = supplied["split_fractions"]
            if not isinstance(sf, dict):
                raise DomainError("wizard.split_fractions_not_object", status=422)
            try:
                train = float(sf.get("train", 0.7))
                val = float(sf.get("val", 0.15))
                test = float(sf.get("test", 0.15))
            except (TypeError, ValueError) as exc:
                raise DomainError("wizard.split_fractions_not_number", status=422, error=str(exc)) from exc
            if any(v < 0 for v in (train, val, test)):
                raise DomainError("wizard.split_fractions_negative", status=422)
            total = train + val + test
            if abs(total - 1.0) > 1e-6:
                raise DomainError("wizard.split_fractions_sum", status=422, total=f"{total:.4f}")
            patch["split_fractions"] = {"train": train, "val": val, "test": test}

        if "seed" in supplied and supplied["seed"] is not None:
            patch["seed"] = int(supplied["seed"])

        # Unlike the other fields an explicit null is meaningful here (remove
        # the limit), and the patch carries cents to match ``max_cost_cents``.
        if "spending_limit_usd" in supplied:
            dollars = supplied["spending_limit_usd"]
            patch["max_cost_cents"] = None if dollars is None else max(1, round(dollars * 100))

        return WizardUpdateResponse(wizard_state=patch)

    return router

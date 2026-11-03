"""Inbound optimization payloads plus the initial submission ack."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from ..brand import APP_NAME
from .common import ColumnMapping, ModelConfig, OptimizationStatus, OptimizationType, SplitFractions
from .results import ModelTokenUsage
from .serve import WorkflowNodeTrace
from .workflow import WORKFLOW_MODULE_NAME, WorkflowSpec, workflow_tool_users


# Where a react run sources its tool roster: a live MCP endpoint or a snapshot
# carried alongside the dataset.
class ToolSource(BaseModel):
    kind: Literal["live_mcp", "dataset_snapshot"]
    mcp_url: str | None = None
    mcp_auth_header: str | None = None
    tool_filter: list[str] | None = None


class _OptimizationRequestBase(BaseModel):
    """Shared fields for all optimization submissions."""

    # ``model_config`` here is the Pydantic class-config attr; ``RunRequest``
    # additionally exposes a wire-aliased field whose alias is also
    # ``model_config`` (the OpenAPI property name the frontend sends), so the
    # same identifier intentionally serves two purposes — class config here
    # vs. field alias on the subclass.
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, description="User-defined display name for this optimization.")
    description: str | None = Field(
        default=None, max_length=280, description="Short description of the optimization goal (max 280 characters)."
    )
    username: str | None = Field(
        default=None,
        description=(
            "Submitter identity. Optional on the wire — the API always overwrites it from the authenticated session, "
            "so clients (including MCP tool callers) can omit it."
        ),
    )
    module_name: str
    module_kwargs: dict[str, Any] = Field(default_factory=dict)
    signature_code: str | None = Field(
        default=None,
        description=(
            "The single dspy.Signature the module wraps. Required for every module except "
            "'workflow', whose per-node signatures live inside the workflow spec instead."
        ),
    )
    workflow: WorkflowSpec | None = Field(
        default=None,
        description=(
            "Workflow graph spec. Required when module_name is 'workflow', forbidden otherwise. "
            "The graph's input-anchor fields are the run's input ports (covered by "
            "column_mapping.inputs) and its output-anchor fields are the final outputs the "
            "metric scores (covered by column_mapping.outputs)."
        ),
    )
    metric_code: str | None = None
    optimizer_name: str
    optimizer_kwargs: dict[str, Any] = Field(default_factory=dict)
    compile_kwargs: dict[str, Any] = Field(default_factory=dict)
    # max_length matches the staging cap (StageDatasetForAgentRequest) — an
    # uncapped inline list lets a single submit balloon the shared API process.
    dataset: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=200_000,
        description=(
            "Inline dataset rows. Optional when ``staged_dataset_id`` is provided — the server then loads the rows "
            "from the staged copy. Exactly one of ``dataset`` or ``staged_dataset_id`` must be present."
        ),
    )
    staged_dataset_id: str | None = Field(
        default=None,
        description=(
            "Opaque id returned by ``POST /datasets/stage-for-agent``. Used by agent-driven submits so the model "
            "does not have to inline tens of thousands of dataset rows into its tool arguments."
        ),
    )
    source_dataset_id: str | None = Field(
        default=None,
        description=(
            "Id of a saved personal-library dataset to run by reference. The server resolves the caller's access, "
            "loads the rows onto ``dataset``, and records the link from the optimization back to the dataset. "
            "Mutually exclusive with ``dataset`` and ``staged_dataset_id``."
        ),
    )
    column_mapping: ColumnMapping
    column_order: list[str] | None = Field(
        default=None,
        description=(
            "Dataset column names in the order the user arranged them at submit time. "
            "Persisted as an array because JSONB does not preserve object key order — a "
            "clone reads this back to restore the original column order in the UI."
        ),
    )
    split_fractions: SplitFractions = Field(default_factory=SplitFractions)
    shuffle: bool = True
    seed: int | None = None
    dataset_filename: str | None = Field(default=None, description="Original dataset file name.")
    is_private: bool = Field(
        default=False,
        description="When true, the optimization is excluded from the public explore page.",
    )
    token_source: Literal["managed", "byok", "subscription"] = Field(
        default="managed",
        description=(
            f"How model calls are billed: 'managed' charges the marked-up provider cost to the {APP_NAME} balance; "
            "'byok' sends calls through the user's provider key without a Skynet model fee. "
            "Billable sandbox usage is charged at marked-up provider cost in both modes."
        ),
    )
    execution_runtime: Literal["vercel", "docker"] = "vercel"
    preflight_id: str | None = Field(default=None, min_length=1, max_length=64)
    preflight_fingerprint: str | None = Field(default=None, min_length=1, max_length=128)
    execution_budget_id: str | None = Field(default=None, min_length=1, max_length=64)
    execution_budget_revision: int | None = Field(default=None, ge=1)
    execution_budget_generation: int | None = Field(default=None, ge=0)
    max_cost_cents: int | None = Field(
        validation_alias=AliasChoices("max_cost_cents", "max_cost_credits"),
        default=None,
        ge=1,
        description=(
            "User-set per-job spend ceiling, in cents. A DSPy optimizer's token use is not "
            "linear (bootstrapping, compile steps, validation loops), so the wizard shows a "
            "projected bracket rather than a tight estimate and lets the user cap the run here. "
            "The run is hard-stopped server-side once its accumulated cost reaches this "
            "cap; consumed work remains billed and the run preserves any evaluated result. "
            "Omit (null) for no ceiling."
        ),
    )
    target_score: float | None = Field(
        default=None,
        gt=0,
        le=100,
        description=(
            "Optional validation score target, expressed as a percentage. GEPA stops searching "
            "when its best validation candidate reaches this score; the existing metric-call "
            "budget remains a safety ceiling."
        ),
    )
    economy_mode: bool = Field(
        default=False,
        description=(
            "Send the run's managed model calls through OpenRouter's Batch API at half the token "
            "price. Each round of calls waits for its batch, typically minutes and at most a day, "
            "so the run finishes much later. Calls on the user's own OpenRouter key run normally."
        ),
    )
    estimated_cents_low: int | None = Field(
        validation_alias=AliasChoices("estimated_cents_low", "estimated_credits_low"),
        default=None,
        ge=0,
        description=(
            "Low end of the projected cost bracket (cents) the wizard showed at submit. Persisted so "
            "the post-run proof moment can reconcile the estimate against the actual charge. "
            "Carries the chargeable bracket for the run's token_source (managed: full per-model "
            "cost; byok: no model fee). Advisory only — never gates or bills. Omit (null) when "
            "no estimate was computed."
        ),
    )
    estimated_cents_high: int | None = Field(
        validation_alias=AliasChoices("estimated_cents_high", "estimated_credits_high"),
        default=None,
        ge=0,
        description=(
            "High end of the projected cost bracket (see estimated_cents_low). Seeds the "
            "post-run estimate-vs-actual reconciliation. Advisory only."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_execution_runtime(cls, data: Any) -> Any:
        """Map the retired worker selection onto the managed sandbox.

        Args:
            data: Raw request data before field validation.

        Returns:
            A copied mapping with the canonical Vercel runtime when the
            retired worker value was supplied, otherwise the input unchanged.
        """
        if isinstance(data, dict) and data.get("execution_runtime") == "worker":
            return {**data, "execution_runtime": "vercel"}
        return data

    @model_validator(mode="after")
    def _ensure_dataset(self) -> _OptimizationRequestBase:
        """Require exactly one dataset source: inline rows, a staged id, or a library id.

        Returns:
            The validated request instance.

        Raises:
            ValueError: When more than one of ``dataset``, ``staged_dataset_id``,
                and ``source_dataset_id`` is supplied, or when none is.
        """
        provided = sum((bool(self.dataset), bool(self.staged_dataset_id), bool(self.source_dataset_id)))
        if provided > 1:
            raise ValueError("Provide exactly one of dataset, staged_dataset_id, or source_dataset_id.")
        if provided == 0:
            raise ValueError(
                "Dataset must contain at least one row, or staged_dataset_id / source_dataset_id must be provided."
            )
        return self

    @model_validator(mode="after")
    def _ensure_module_shape(self) -> _OptimizationRequestBase:
        """Pair ``module_name`` with the matching program definition.

        Workflow runs carry their signatures per node inside ``workflow``;
        every other module wraps exactly one top-level ``signature_code``.

        Returns:
            The validated request instance.

        Raises:
            ValueError: When a workflow run lacks ``workflow``, a non-workflow
                run lacks ``signature_code``, or ``workflow`` is supplied for a
                non-workflow module.
        """
        if self.module_name.lower() == WORKFLOW_MODULE_NAME:
            if self.workflow is None:
                raise ValueError("workflow is required when module_name is 'workflow'.")
        else:
            if self.workflow is not None:
                raise ValueError("workflow is only valid when module_name is 'workflow'.")
            if not self.signature_code:
                raise ValueError("signature_code is required.")
        return self


class RunRequest(_OptimizationRequestBase):
    """Payload for the /run endpoint."""

    model_settings: ModelConfig = Field(alias="model_config")
    reflection_model_settings: ModelConfig | None = Field(default=None, alias="reflection_model_config")
    task_model_settings: ModelConfig | None = Field(default=None, alias="task_model_config")
    tool_source: ToolSource | None = None

    @model_validator(mode="after")
    def _require_metric_code(self) -> RunRequest:
        """Re-require ``metric_code`` for every run, including react.

        ``metric_code`` is declared optional on the base only so the field can be
        shared; every run must supply it. React is a generic module that scores
        rollouts with the same standard ``(gold, pred, trace, pred_name,
        pred_trace)`` metric the predict/cot path uses.

        Returns:
            The validated request instance.

        Raises:
            ValueError: When ``metric_code`` is missing.
        """
        if self.metric_code is None:
            raise ValueError("metric_code is required.")
        return self

    @model_validator(mode="after")
    def _require_tool_source_for_workflow_tools(self) -> RunRequest:
        """Require a run-level tool roster when the graph contains tool-using nodes.

        Workflow react, flex and mcp nodes resolve their tools from the run's
        single ``tool_source`` (optionally narrowed per node), mirroring how
        a top-level react run sources its roster.

        Returns:
            The validated request instance.

        Raises:
            ValueError: When the workflow has tool-using nodes but no
                ``tool_source`` is supplied.
        """
        if self.workflow is not None and self.tool_source is None:
            tool_users = workflow_tool_users(self.workflow)
            if tool_users:
                raise ValueError(f"tool_source is required — these workflow nodes use tools: {tool_users}.")
        return self


class WorkflowDryRunRequest(BaseModel):
    """Request payload for POST /workflows/dry-run — one unoptimized test execution."""

    model_config = ConfigDict(populate_by_name=True)

    workflow: WorkflowSpec
    inputs: dict[str, Any] = Field(description="Values for the workflow's input-anchor fields.")
    model_settings: ModelConfig = Field(alias="model_config")
    tool_source: ToolSource | None = None
    execution_runtime: Literal["vercel", "docker"] = "vercel"
    execution_budget_id: str | None = Field(default=None, min_length=1, max_length=64)
    execution_budget_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _normalize_execution_runtime(cls, data: Any) -> Any:
        """Map the retired worker selection onto the managed sandbox.

        Args:
            data: Raw request data before field validation.

        Returns:
            A copied mapping with the canonical Vercel runtime when the
            retired worker value was supplied, otherwise the input unchanged.
        """
        if isinstance(data, dict) and data.get("execution_runtime") == "worker":
            return {**data, "execution_runtime": "vercel"}
        return data

    @model_validator(mode="after")
    def _require_tool_source_for_tools(self) -> WorkflowDryRunRequest:
        """Require a tool roster when the graph contains tool-using nodes.

        Returns:
            The validated request instance.

        Raises:
            ValueError: When tool-using nodes exist but no ``tool_source``.
        """
        if self.tool_source is None:
            tool_users = workflow_tool_users(self.workflow)
            if tool_users:
                raise ValueError(f"tool_source is required — these workflow nodes use tools: {tool_users}.")
        return self


class WorkflowDryRunResponse(BaseModel):
    """Result of a workflow dry run.

    A node failure is an expected outcome the canvas renders, not an HTTP
    error: the response carries the failing node id, the error, and every
    trace collected up to (and including) the failure.
    """

    outputs: dict[str, Any] | None = None
    node_traces: list[WorkflowNodeTrace] = Field(default_factory=list)
    model_used: str
    error: str | None = None
    failed_node_id: str | None = None
    usage_by_model: list[ModelTokenUsage] = Field(default_factory=list)
    cents_charged: int = 0
    budget: dict[str, Any] | None = None
    preview_status: Literal["succeeded", "failed", "pending"] | None = None
    preflight_id: str | None = None


class OptimizationSubmissionResponse(BaseModel):
    """Immediate response to POST /run or POST /grid-search."""

    optimization_id: str
    optimization_type: OptimizationType
    status: OptimizationStatus
    created_at: datetime
    name: str | None = None
    description: str | None = None
    username: str
    module_name: str
    optimizer_name: str

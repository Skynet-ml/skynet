"""Inbound optimization payloads plus the initial submission ack."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import ModelConfig, OptimizationStatus, OptimizationType
from .results import ModelTokenUsage
from .serve import WorkflowNodeTrace
from .workflow import WorkflowSpec, workflow_tool_users


# Where a react run sources its tool roster: a live MCP endpoint or a snapshot
# carried alongside the dataset.
class ToolSource(BaseModel):
    kind: Literal["live_mcp", "dataset_snapshot"]
    mcp_url: str | None = None
    mcp_auth_header: str | None = None
    tool_filter: list[str] | None = None


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

"""Pydantic request and response models local to the optimizations routes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ....models import OptimizationSubmissionResponse


class SidebarJobItem(BaseModel):
    """Compact per-optimization entry for the sidebar navigation list."""

    optimization_id: str
    status: str
    name: str | None = None
    module_name: str | None = None
    optimizer_name: str | None = None
    model_name: str | None = None
    username: str | None = None
    created_at: datetime | None = None
    pinned: bool = False
    optimization_type: str | None = None
    # True when this run stopped mid-optimization with a saved checkpoint and can
    # be resumed in place; drives the sidebar Resume-vs-Restart menu item.
    resumable: bool = False
    pausable: bool = False
    # Caller's share role on this run ("viewer"/"editor"/"owner"); only set on
    # the "shared with me" listing, None for the caller's own optimizations.
    role: str | None = None
    # Folder the run is filed in (each run has at most one), or None when unfiled.
    folder_id: str | None = None
    # True when the run belongs to a continuous plan: enabled from it, its
    # current version, or started by one of its scheduled checks.
    automated: bool = False


class SidebarJobsResponse(BaseModel):
    """Paginated response for the sidebar optimization list."""

    items: list[SidebarJobItem]
    total: int


class CloneJobResponse(BaseModel):
    """List of newly-created clones plus the source id for reference."""

    source_optimization_id: str
    created: list[OptimizationSubmissionResponse]


class BulkMetadataRequest(BaseModel):
    """Request body for bulk pin."""

    optimization_ids: list[str] = Field(min_length=1, max_length=100)
    value: bool = Field(description="Target state — true to pin, false to clear.")


class BulkMetadataSkipped(BaseModel):
    """An optimization that was omitted from a bulk update, with a human-readable reason."""

    optimization_id: str
    reason: str


class BulkMetadataResponse(BaseModel):
    """Response for POST /optimizations/bulk-pin."""

    updated: list[str]
    skipped: list[BulkMetadataSkipped]

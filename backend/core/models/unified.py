"""Shared wizard models: optimizer choices, cases and models.

Skynet no longer runs the unified DSPy program payload (SKY-316, SKY-656),
so its request models are gone. What stays serves repository runs: the
optimizer catalog, :class:`UnifiedCase` and :class:`UnifiedModels` (the
model roles that repository billing resolves).

Pydantic class docstrings are part of the OpenAPI contract — see AGENTS.md
"Pydantic class docstrings" — so per-model annotations live in comments
above each class, not in class bodies.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .blackbox import UnifiedSplitCounts
from .common import ModelConfig

DataMode = Literal["single", "pairs"]
OptimizerChoice = Literal[
    "auto",
    "gepa",
    "best_of_n",
    "autoresearch",
    "meta_harness",
    "autosaddler",
    "shinka_evolve",
]
OptimizerFamily = Literal["auto", "blackbox"]
BLACKBOX_OPTIMIZERS: tuple[str, ...] = (
    "gepa",
    "best_of_n",
    "autoresearch",
    "meta_harness",
    "autosaddler",
    "shinka_evolve",
)
# "auto" is listed because the black-box Auto strategy continues with GEPA,
# which consumes feedback text.
FEEDBACK_OPTIMIZERS = frozenset({"gepa", "auto"})
MIN_PAIRS_FOR_HELD_OUT = 5
OPTIMIZER_AUTO = "auto"


def optimizers_for(data_mode: DataMode) -> list[OptimizerChoice]:
    """List the optimizer choices a new run may pick for a data mode.

    Every run optimizes a repository (SKY-316), so the list is the same for
    every run.

    Runtime availability (sandbox, installed engines) is filtered separately
    by the catalog route; this is the static relevance rule.

    Args:
        data_mode: Whether the run has one input or input/output pairs.

    Returns:
        ``"auto"`` first, then the black-box engines.
    """
    del data_mode
    return [OPTIMIZER_AUTO, *BLACKBOX_OPTIMIZERS]


def optimizer_family(resolved: str) -> OptimizerFamily:
    """Name the family an optimizer belongs to.

    Args:
        resolved: An optimizer id.

    Returns:
        ``"auto"`` for the black-box Auto strategy, otherwise ``"blackbox"``.
    """
    if resolved == OPTIMIZER_AUTO:
        return "auto"
    return "blackbox"


# The coding agent's contract, its validators and the editors' hints all read
# these, so what the agent is told and what a person writing by hand is shown
# never drift apart. Every artifact kind and every optimizer uses this pair;
# the engines adapt to it internally.
RUN_ENTRY = "def run(artifact, input)"
METRIC_SIGNATURE = "def score(input, output, expected=None)"


# One case. ``expected`` reaches the metric's ``expected`` argument as is.
class UnifiedCase(BaseModel):
    input: dict[str, Any]
    expected: Any | None = None


# ``task_model`` is what ``skynet.llm`` calls inside run code;
# ``metric_model`` is derived from the metric code and used inside the metric.
class UnifiedModels(BaseModel):
    task_model: ModelConfig | None = None
    reflection_model: ModelConfig
    extra_reflection_models: list[ModelConfig] = Field(default_factory=list, max_length=4)
    metric_model: ModelConfig | None = None


class OptimizerOption(BaseModel):
    id: OptimizerChoice
    family: OptimizerFamily
    available: bool
    unavailable_reason: str | None = None
    needs_reflection_model: bool
    uses_feedback: bool
    supports_iteration_limit: bool
    native_proposer: bool


# ``GET /optimizers``. ``auto_resolves_to`` lists what Auto runs.
# ``run_entry`` and ``metric_signature`` are the one contract every optimizer runs.
class OptimizerCatalogResponse(BaseModel):
    data_mode: DataMode
    run_entry: str
    metric_signature: str
    default: OptimizerChoice = "auto"
    options: list[OptimizerOption]
    auto_resolves_to: list[str]
    sandbox_available: bool
    sandbox_reason: str | None = None


__all__ = [
    "BLACKBOX_OPTIMIZERS",
    "FEEDBACK_OPTIMIZERS",
    "METRIC_SIGNATURE",
    "MIN_PAIRS_FOR_HELD_OUT",
    "RUN_ENTRY",
    "DataMode",
    "OptimizerCatalogResponse",
    "OptimizerChoice",
    "OptimizerFamily",
    "OptimizerOption",
    "UnifiedCase",
    "UnifiedModels",
    "UnifiedSplitCounts",
    "optimizer_family",
    "optimizers_for",
]

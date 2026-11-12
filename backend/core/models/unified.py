"""Shared wizard models: optimizer choices, cases, models and workspace rules.

Skynet no longer runs the unified DSPy program payload (SKY-316, SKY-656),
so its request models are gone. What stays serves repository runs: the
optimizer catalog, :class:`UnifiedCase`, :class:`UnifiedModels` (the model
roles that repository billing resolves) and the workspace file rules.

Pydantic class docstrings are part of the OpenAPI contract — see AGENTS.md
"Pydantic class docstrings" — so per-model annotations live in comments
above each class, not in class bodies.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .blackbox import UnifiedSplitCounts
from .common import ModelConfig
from .scorer_dependencies import ScorerDependencyLock

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

# The workspace's two fixed files. Their text travels as ``run_code`` and
# ``metric_code`` so payloads written before extra files existed stay valid;
# ``UnifiedWorkspace.files`` holds every other file.
RUN_FILE = "run.py"
METRIC_FILE = "metric.py"
WORKSPACE_FILE_MAX_COUNT = 20
WORKSPACE_FILE_MAX_CHARS = 200_000
WORKSPACE_FILES_MAX_TOTAL_CHARS = 500_000
WORKSPACE_PATH_MAX_CHARS = 120
WORKSPACE_PATH_MAX_DEPTH = 4
WORKSPACE_FILE_SUFFIXES = (".py", ".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".toml", ".ini", ".cfg")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
# ``skynet`` is the helper module the harness injects; a file under that
# name would silently lose to it.
_RESERVED_TOP_LEVEL = frozenset({RUN_FILE, METRIC_FILE, "skynet", "skynet.py"})


def workspace_path_error(path: str) -> str | None:
    """Explain why ``path`` cannot name an extra workspace file.

    Args:
        path: A file path as the user or the coding agent wrote it.

    Returns:
        A short English reason, or ``None`` when the path is acceptable:
        relative, ``/``-separated, at most :data:`WORKSPACE_PATH_MAX_DEPTH`
        segments of letters, digits, ``_``, ``-`` and ``.`` (none hidden or
        ``..``), ending in a Python or text suffix, and not one of the fixed
        files or the ``skynet`` helper.
    """
    if not path or len(path) > WORKSPACE_PATH_MAX_CHARS:
        return f"the path must be 1-{WORKSPACE_PATH_MAX_CHARS} characters"
    if path.startswith("/") or "\\" in path or ":" in path:
        return "the path must be relative, with / between folders"
    segments = path.split("/")
    if len(segments) > WORKSPACE_PATH_MAX_DEPTH:
        return f"the path may be at most {WORKSPACE_PATH_MAX_DEPTH} levels deep"
    if any(segment in ("", ".", "..") or not _PATH_SEGMENT.fullmatch(segment) for segment in segments):
        return "each part of the path may use letters, digits, _, - and . only, and no part may be empty or .."
    if segments[0] in _RESERVED_TOP_LEVEL:
        return f"{segments[0]} is reserved"
    if not path.endswith(WORKSPACE_FILE_SUFFIXES):
        return f"the file must end in one of {', '.join(WORKSPACE_FILE_SUFFIXES)}"
    return None


def workspace_files_error(files: dict[str, str]) -> str | None:
    """Explain why a set of extra workspace files cannot be stored.

    Args:
        files: Path to text of every extra file.

    Returns:
        The first problem found, or ``None`` when every path is valid and the
        files keep to the count and size limits.
    """
    if len(files) > WORKSPACE_FILE_MAX_COUNT:
        return f"a workspace holds at most {WORKSPACE_FILE_MAX_COUNT} extra files"
    total = 0
    for path, text in files.items():
        error = workspace_path_error(path)
        if error is not None:
            return f"{path}: {error}"
        if len(text) > WORKSPACE_FILE_MAX_CHARS:
            return f"{path}: a file may hold at most {WORKSPACE_FILE_MAX_CHARS} characters"
        total += len(text)
    if total > WORKSPACE_FILES_MAX_TOTAL_CHARS:
        return f"the extra files may hold at most {WORKSPACE_FILES_MAX_TOTAL_CHARS} characters together"
    prefixes = {"/".join(path.split("/")[:depth]) for path in files for depth in range(1, path.count("/") + 1)}
    clash = sorted(prefixes & set(files))
    if clash:
        return f"{clash[0]} is both a file and a folder"
    return None


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


# The workspace code. ``metric_code`` never reaches an optimizer: it travels
# only here and in the engine Task's private context. ``run_code`` and
# ``metric_code`` are ``run.py`` and ``metric.py``; ``files`` maps every other
# file's relative path to its text. Only the run code can import them: the
# harness puts them on the run child's path, never the metric's.
class UnifiedWorkspace(BaseModel):
    run_code: str = Field(min_length=1, max_length=200_000)
    metric_code: str = Field(min_length=1, max_length=200_000)
    files: dict[str, str] = Field(default_factory=dict)
    metric_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    install_command: str | None = None
    dependency_lock: ScorerDependencyLock | None = None

    @model_validator(mode="after")
    def _check_files(self) -> UnifiedWorkspace:
        """Reject extra files with unsafe paths or past the size limits.

        Returns:
            The workspace unchanged.

        Raises:
            ValueError: With the first problem found.
        """
        error = workspace_files_error(self.files)
        if error is not None:
            raise ValueError(error)
        return self


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
    "METRIC_FILE",
    "METRIC_SIGNATURE",
    "MIN_PAIRS_FOR_HELD_OUT",
    "RUN_ENTRY",
    "RUN_FILE",
    "WORKSPACE_FILE_MAX_COUNT",
    "DataMode",
    "OptimizerCatalogResponse",
    "OptimizerChoice",
    "OptimizerFamily",
    "OptimizerOption",
    "UnifiedCase",
    "UnifiedModels",
    "UnifiedSplitCounts",
    "UnifiedWorkspace",
    "optimizer_family",
    "optimizers_for",
    "workspace_files_error",
    "workspace_path_error",
]

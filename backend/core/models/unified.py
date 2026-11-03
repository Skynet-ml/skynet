"""Request/response models for the unified (wizard v2) optimization run.

One payload covers every optimizer: the DSPy optimizers and the black-box
engines all run through the black-box engine layer against the same workspace
contract (a ``run(artifact, input)`` function plus a ``score(input, output,
expected=None)`` metric, whatever the artifact kind or optimizer). Stored runs keep the ``blackbox``
optimization type and are told apart from legacy black-box runs by
``payload_kind``.

Pydantic class docstrings are part of the OpenAPI contract — see AGENTS.md
"Pydantic class docstrings" — so per-model annotations live in comments
above each class, not in class bodies.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ..i18n_en import t_en
from ..i18n_keys import I18nKey
from .blackbox import (
    BlackboxBudget,
    BlackboxCandidate,
    BlackboxProposer,
    BlackboxShinkaSettings,
    UnifiedSplitCounts,
)
from .common import ModelConfig, SplitFractions
from .scorer_dependencies import ScorerDependencyLock

UNIFIED_PAYLOAD_KIND = "unified_v1"
# Every path rides each agent turn's prompt, so a repository's list is capped.
REPO_FILES_MAX = 2000
REPO_FILE_PATH_MAX = 1024

ArtifactKind = Literal["prompt", "code", "dspy_program", "text"]
DataMode = Literal["single", "pairs"]
OptimizerChoice = Literal[
    "auto",
    "dspy_gepa",
    "dspy_miprov2",
    "dspy_bootstrap_fewshot",
    "gepa",
    "best_of_n",
    "autoresearch",
    "meta_harness",
    "autosaddler",
    "shinka_evolve",
]
OptimizerFamily = Literal["auto", "dspy", "blackbox"]
DSPY_OPTIMIZERS: tuple[str, ...] = ("dspy_gepa", "dspy_miprov2", "dspy_bootstrap_fewshot")
BLACKBOX_OPTIMIZERS: tuple[str, ...] = (
    "gepa",
    "best_of_n",
    "autoresearch",
    "meta_harness",
    "autosaddler",
    "shinka_evolve",
)
# instantiate_optimizer() resolves names through resolve_optimizer_factory():
# only GEPA has a short alias, the others resolve by their dotted dspy path.
DSPY_OPTIMIZER_NAMES: dict[str, str] = {
    "dspy_gepa": "gepa",
    "dspy_miprov2": "dspy.teleprompt.MIPROv2",
    "dspy_bootstrap_fewshot": "dspy.teleprompt.BootstrapFewShot",
}
# "auto" is listed because both of its resolutions (DSPy GEPA, the black-box
# Auto strategy that continues with GEPA) consume feedback text.
FEEDBACK_OPTIMIZERS = frozenset({"dspy_gepa", "gepa", "auto"})
MIN_PAIRS_FOR_HELD_OUT = 5
CONTRACT_SAMPLE_MAX = 5
# How many samples a sandboxed contract check scores when the caller names none.
CONTRACT_SAMPLE_DEFAULT = 3
# Canonical-payload key carrying a contract check's sample count from the API
# into the setup sandbox; public so it is part of the evidence fingerprint.
CONTRACT_SAMPLE_COUNT_KEY = "contract_sample_count"
ARTIFACT_KIND_DSPY_PROGRAM = "dspy_program"
OPTIMIZER_AUTO = "auto"


def optimizers_for(artifact_kind: ArtifactKind, data_mode: DataMode) -> list[OptimizerChoice]:
    """List the optimizer choices that fit an artifact kind and data mode.

    Runtime availability (sandbox, installed engines) is filtered separately
    by the catalog route; this is the static relevance rule.

    Args:
        artifact_kind: What is being optimized.
        data_mode: Whether the run has one input or input/output pairs.

    Returns:
        The allowed choices, ``"auto"`` first.
    """
    if artifact_kind == ARTIFACT_KIND_DSPY_PROGRAM:
        if data_mode == "pairs":
            return ["auto", "dspy_gepa", "dspy_miprov2", "dspy_bootstrap_fewshot"]
        return ["auto", "dspy_gepa"]
    return ["auto", "gepa", "best_of_n", "autoresearch", "meta_harness", "autosaddler", "shinka_evolve"]


def resolve_optimizer(choice: OptimizerChoice, artifact_kind: ArtifactKind) -> str:
    """Resolve the user's choice to the optimizer that actually runs.

    Args:
        choice: The optimizer the user picked.
        artifact_kind: What is being optimized.

    Returns:
        ``"dspy_gepa"`` for Auto on a DSPy program, ``"auto"`` (the black-box
        Auto strategy) for Auto on anything else, otherwise ``choice``.
    """
    if choice == OPTIMIZER_AUTO:
        return "dspy_gepa" if artifact_kind == ARTIFACT_KIND_DSPY_PROGRAM else OPTIMIZER_AUTO
    return choice


def optimizer_family(resolved: str) -> OptimizerFamily:
    """Name the family a resolved optimizer belongs to.

    Args:
        resolved: An optimizer id as returned by :func:`resolve_optimizer`.

    Returns:
        ``"dspy"`` for the DSPy optimizers, ``"auto"`` for the black-box Auto
        strategy, otherwise ``"blackbox"``.
    """
    if resolved in DSPY_OPTIMIZERS:
        return "dspy"
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


# The thing under optimization and where it starts.
class UnifiedArtifact(BaseModel):
    kind: ArtifactKind
    starting_version: BlackboxCandidate


class UnifiedTask(BaseModel):
    objective: str = Field(min_length=1, max_length=8000)
    background: str = Field(default="", max_length=20000)
    artifact: UnifiedArtifact


# ``expected_field`` names the table column the expected output came from
# when pairs were built from a dataset.
class UnifiedData(BaseModel):
    mode: DataMode
    cases: list[UnifiedCase] | None = Field(default=None, max_length=200_000)
    staged_dataset_id: str | None = Field(default=None, min_length=1, max_length=64)
    input_fields: list[str] = Field(default_factory=list)
    expected_field: str | None = None
    split: SplitFractions = Field(default_factory=SplitFractions)
    shuffle: bool = True

    @model_validator(mode="after")
    def _ensure_cases(self) -> UnifiedData:
        """Require cases (or a staged dataset) that fit the data mode.

        Returns:
            The validated data instance.

        Raises:
            ValueError: When neither cases nor a staged dataset is given, when
                single mode does not carry exactly one case, or when pairs mode
                carries fewer than two cases.
        """
        if self.cases is None:
            if self.staged_dataset_id is None:
                key = (
                    I18nKey.SUBMIT_UNIFIED_SINGLE_NEEDS_ONE_CASE
                    if self.mode == "single"
                    else I18nKey.SUBMIT_UNIFIED_PAIRS_NEED_CASES
                )
                raise ValueError(t_en(key))
            return self
        if self.mode == "single" and len(self.cases) != 1:
            raise ValueError(t_en(I18nKey.SUBMIT_UNIFIED_SINGLE_NEEDS_ONE_CASE))
        if self.mode == "pairs" and len(self.cases) < 2:
            raise ValueError(t_en(I18nKey.SUBMIT_UNIFIED_PAIRS_NEED_CASES))
        return self


# ``dspy_kwargs`` reaches the DSPy optimizer constructor; black-box engines
# ignore it, and DSPy optimizers ignore ``proposer`` and ``shinka``.
class UnifiedOptimizer(BaseModel):
    choice: OptimizerChoice = "auto"
    budget: BlackboxBudget = Field(default_factory=BlackboxBudget)
    proposer: BlackboxProposer = Field(default_factory=BlackboxProposer)
    shinka: BlackboxShinkaSettings | None = None
    dspy_kwargs: dict[str, Any] = Field(default_factory=dict)


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


# Submission payload for ``POST /optimize``.
class UnifiedRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    payload_kind: Literal["unified_v1"] = "unified_v1"
    name: str | None = None
    description: str | None = Field(default=None, max_length=280)
    username: str | None = None
    task: UnifiedTask
    data: UnifiedData
    optimizer: UnifiedOptimizer = Field(default_factory=UnifiedOptimizer)
    models: UnifiedModels
    workspace: UnifiedWorkspace
    seed: int | None = None
    token_source: Literal["managed", "byok", "subscription"] = "managed"
    is_private: bool = False
    economy_mode: bool = False
    preflight_id: str | None = Field(default=None, min_length=1, max_length=64)
    preflight_fingerprint: str | None = Field(default=None, min_length=1, max_length=128)
    execution_budget_id: str | None = Field(default=None, min_length=1, max_length=64)
    execution_budget_revision: int | None = Field(default=None, ge=1)
    execution_budget_generation: int | None = Field(default=None, ge=0)
    max_cost_cents: int | None = Field(
        validation_alias=AliasChoices("max_cost_cents", "max_cost_credits"), default=None, ge=1
    )
    estimated_cents_low: int | None = Field(
        validation_alias=AliasChoices("estimated_cents_low", "estimated_credits_low"), default=None, ge=0
    )
    estimated_cents_high: int | None = Field(
        validation_alias=AliasChoices("estimated_cents_high", "estimated_credits_high"), default=None, ge=0
    )

    @model_validator(mode="after")
    def _ensure_optimizer_allowed(self) -> UnifiedRunRequest:
        """Reject an optimizer that does not fit the artifact kind and data mode.

        Returns:
            The validated request instance.

        Raises:
            ValueError: When ``optimizer.choice`` is not among
                :func:`optimizers_for` for this artifact kind and data mode.
        """
        allowed = optimizers_for(self.task.artifact.kind, self.data.mode)
        if self.optimizer.choice not in allowed:
            raise ValueError(
                t_en(
                    I18nKey.SUBMIT_UNIFIED_OPTIMIZER_NOT_ALLOWED,
                    optimizer=self.optimizer.choice,
                    allowed=", ".join(allowed),
                )
            )
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


# ``GET /optimizers``. ``auto_resolves_to`` lists what Auto runs: DSPy GEPA
# for a DSPy program, otherwise the engines the Auto strategy explores.
# ``run_entry`` and ``metric_signature`` are the one contract every optimizer runs.
class OptimizerCatalogResponse(BaseModel):
    artifact_kind: ArtifactKind
    data_mode: DataMode
    run_entry: str
    metric_signature: str
    default: OptimizerChoice = "auto"
    options: list[OptimizerOption]
    auto_resolves_to: list[str]
    sandbox_available: bool
    sandbox_reason: str | None = None


ContractCheckKey = Literal[
    "workspace.run_signature",
    "workspace.files",
    "metric.signature",
    "metric.model",
    "workspace.run",
    "metric.shape",
]


# ``message`` is rendered in the request's ``locale`` (the Hebrew base when it
# names none); ``message_code`` and ``message_params`` are the i18n code and
# placeholders the client renders in its own locale.
# ``detail`` is the exact error text, trimmed to frames from the user's own code.
class ContractCheckItem(BaseModel):
    key: ContractCheckKey
    status: Literal["passed", "failed", "pending", "skipped"]
    message: str
    message_code: str | None = None
    message_params: dict[str, str | int] = Field(default_factory=dict)
    sample_index: int | None = None
    detail: str | None = Field(default=None, max_length=4000)


class ContractSample(BaseModel):
    index: int
    input_preview: str
    output_preview: str | None = None
    score: float | None = None
    feedback_preview: str | None = None
    duration_ms: int | None = None
    # The trusted measurement of this run (see ``workspace_harness.build_observation``).
    observation: dict[str, Any] | None = None


class ContractCheckRequest(BaseModel):
    spec: UnifiedRunRequest
    sample_count: int = Field(default=CONTRACT_SAMPLE_DEFAULT, ge=1, le=CONTRACT_SAMPLE_MAX)
    locale: str | None = Field(default=None, max_length=35)


# One workspace file's lint from the static check: ruff's error rules
# (undefined names, broken imports) as ``errors``, its other findings as
# ``warnings``, each as ``"<path>:<line>:<col> <rule> <message>"``.
class WorkspaceFileLint(BaseModel):
    path: str
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ``fingerprint`` is the sha256 hex of the canonical JSON of the inputs the
# check depends on, so the wizard can tell when a passing check went stale.
# ``files`` is the static check's lint of each Python file that parses, so
# each editor shows its own findings; lint never decides ``ok``.
# ``uses_llm`` indicates whether the metric uses the helper; its model is derived from the code.
class ContractCheckResponse(BaseModel):
    ok: bool
    optimizer_resolved: str
    uses_llm: bool = False
    checks: list[ContractCheckItem]
    samples: list[ContractSample] = Field(default_factory=list)
    fingerprint: str
    files: list[WorkspaceFileLint] = Field(default_factory=list)
    terminal: dict[str, Any] | None = None


class UnifiedChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


# One clickable answer the task interview offers.
class InterviewOption(BaseModel):
    label: str = Field(max_length=80)
    description: str = Field(default="", max_length=400)


# ``POST /optimizations/task-interview``: one turn of the step-1 interview.
class TaskInterviewRequest(BaseModel):
    turns: list[UnifiedChatTurn] = Field(default_factory=list, max_length=40)
    objective: str = ""
    background: str = ""
    artifact_kind: ArtifactKind | None = None
    starting_version: BlackboxCandidate | None = None
    sample_cases: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    locale: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    # Applies only to a local CLI model that takes fast mode.
    fast_mode: bool = False


# Data of the interview's terminal ``interview_done`` event.
class TaskInterviewTurnResult(BaseModel):
    message: str
    options: list[InterviewOption] = Field(default_factory=list)
    done: bool
    objective: str = ""
    background: str = ""
    artifact_kind: ArtifactKind | None = None
    starting_version: BlackboxCandidate | None = None
    artifact_rationale: str = ""
    model: str | None = None
    served_model: str | None = None


# ``POST /optimizations/infer-inputs``: decide single input vs pairs from the
# task and a sample of the uploaded data.
class InputsInferenceRequest(BaseModel):
    objective: str
    background: str = ""
    artifact_kind: ArtifactKind
    starting_version: BlackboxCandidate | None = None
    columns: list[str] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    row_count: int = Field(default=0, ge=0)
    user_note: str = Field(default="", max_length=4000)
    locale: str | None = None
    model: str | None = None


class InputsInferenceResponse(BaseModel):
    mode: DataMode
    input_fields: list[str]
    expected_field: str | None = None
    single_input: dict[str, Any] | None = None
    rationale: str
    suggested_split: SplitFractions
    held_out_count: int
    warnings: list[str] = Field(default_factory=list)
    model: str | None = None


# What the code agent needs to write run code and a metric for a unified run.
# ``focus`` names which of the two the user is working on. ``target`` is
# ``"repo"`` for a repository run: the optimizer edits the repository itself,
# so there is no run code and the agent writes only the repository scorer.
# ``repo_files`` lists a repository run's file paths, which the agent may open
# read-only.
class UnifiedAuthoringContext(BaseModel):
    task: UnifiedTask
    data_mode: DataMode
    input_fields: list[str] = Field(default_factory=list)
    expected_field: str | None = None
    sample_cases: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    optimizer: OptimizerChoice
    metric_has_model: bool = False
    focus: Literal["run", "metric"] = "run"
    target: Literal["artifact", "repo"] = "artifact"
    repo_files: list[Annotated[str, StringConstraints(min_length=1, max_length=REPO_FILE_PATH_MAX)]] = Field(
        default_factory=list, max_length=REPO_FILES_MAX
    )


__all__ = [
    "BLACKBOX_OPTIMIZERS",
    "CONTRACT_SAMPLE_COUNT_KEY",
    "CONTRACT_SAMPLE_DEFAULT",
    "CONTRACT_SAMPLE_MAX",
    "DSPY_OPTIMIZERS",
    "DSPY_OPTIMIZER_NAMES",
    "FEEDBACK_OPTIMIZERS",
    "METRIC_FILE",
    "METRIC_SIGNATURE",
    "MIN_PAIRS_FOR_HELD_OUT",
    "REPO_FILES_MAX",
    "REPO_FILE_PATH_MAX",
    "RUN_ENTRY",
    "RUN_FILE",
    "UNIFIED_PAYLOAD_KIND",
    "WORKSPACE_FILE_MAX_COUNT",
    "ArtifactKind",
    "ContractCheckItem",
    "ContractCheckKey",
    "ContractCheckRequest",
    "ContractCheckResponse",
    "ContractSample",
    "DataMode",
    "InputsInferenceRequest",
    "InputsInferenceResponse",
    "InterviewOption",
    "OptimizerCatalogResponse",
    "OptimizerChoice",
    "OptimizerFamily",
    "OptimizerOption",
    "TaskInterviewRequest",
    "TaskInterviewTurnResult",
    "UnifiedArtifact",
    "UnifiedAuthoringContext",
    "UnifiedCase",
    "UnifiedChatTurn",
    "UnifiedData",
    "UnifiedModels",
    "UnifiedOptimizer",
    "UnifiedRunRequest",
    "UnifiedSplitCounts",
    "UnifiedTask",
    "UnifiedWorkspace",
    "WorkspaceFileLint",
    "optimizer_family",
    "optimizers_for",
    "resolve_optimizer",
    "workspace_files_error",
    "workspace_path_error",
]

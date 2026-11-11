"""Request/response models for black-box text optimization jobs.

A black-box job optimizes any text artifact (a prompt, a policy, a config
file, a piece of code) against a user-supplied scorer, with no DSPy program
in the loop. The contract mirrors GEPA's ``optimize_anything`` surface —
starting point, cases, scorer, budget — plus execution location and model
routing for the pinned upstream engines and compositions.

Pydantic class docstrings are part of the OpenAPI contract — see AGENTS.md
"Pydantic class docstrings" — so per-model annotations live in comments
above each class, not in class bodies.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifacts import ProgramArtifact
from .common import ModelConfig, refuse_removed_cli
from .results import LMActivity, ModelTokenUsage
from .scorer_dependencies import ScorerDependencyLock

BLACKBOX_ENGINE_GEPA = "gepa"
BLACKBOX_ENGINE_BEST_OF_N = "best_of_n"
BLACKBOX_ENGINE_AUTORESEARCH = "autoresearch"
BLACKBOX_ENGINE_META_HARNESS = "meta_harness"
BLACKBOX_ENGINE_AUTOSADDLER = "autosaddler"
BLACKBOX_ENGINE_SHINKA_EVOLVE = "shinka_evolve"
BLACKBOX_STRATEGY_AUTO = "auto"
BLACKBOX_TARGET_TEXT = "text"
BLACKBOX_TARGET_AGENT = "agent"
BLACKBOX_TARGET_REPO = "repo"
BLACKBOX_HARNESS_PI = "pi"
BLACKBOX_HARNESS_CODEX = "codex"
# Claude Code is closed source, so Skynet neither ships nor runs it. Stored
# runs may still name it; new requests that do are rejected.
BLACKBOX_HARNESS_CLAUDE_CODE = "claude_code"
BLACKBOX_HARNESS_OPENCODE = "opencode"
BLACKBOX_HARNESS_PRIME = "prime"
BLACKBOX_HARNESS_AIDER = "aider"
BLACKBOX_HARNESS_ANTIGRAVITY_SDK = "antigravity_sdk"
BLACKBOX_HARNESS_FX = "fx"
BLACKBOX_HARNESS_CLINE = "cline"
BLACKBOX_HARNESS_GOOSE = "goose"
BLACKBOX_HARNESS_EVE = "eve"
BLACKBOX_HARNESS_CUSTOM = "custom"
BLACKBOX_HARNESSES = (
    BLACKBOX_HARNESS_PI,
    BLACKBOX_HARNESS_CODEX,
    BLACKBOX_HARNESS_OPENCODE,
    BLACKBOX_HARNESS_PRIME,
    BLACKBOX_HARNESS_AIDER,
    BLACKBOX_HARNESS_ANTIGRAVITY_SDK,
    BLACKBOX_HARNESS_FX,
    BLACKBOX_HARNESS_GOOSE,
    BLACKBOX_HARNESS_EVE,
    BLACKBOX_HARNESS_CLINE,
    BLACKBOX_HARNESS_CUSTOM,
)
# Engines that accept a multi-part (named files) starting point.
BLACKBOX_MULTI_PART_ENGINES = frozenset(
    {BLACKBOX_ENGINE_GEPA, BLACKBOX_ENGINE_AUTOSADDLER, BLACKBOX_ENGINE_SHINKA_EVOLVE}
)
# Engines that can optimize a repository: each writes versions against a real
# checkout (a coding agent's edits, or ShinkaEvolve's per-file bundles) and the
# parent scores the resulting patch.
BLACKBOX_REPO_ENGINES = frozenset(
    {
        BLACKBOX_ENGINE_AUTORESEARCH,
        BLACKBOX_ENGINE_GEPA,
        BLACKBOX_ENGINE_BEST_OF_N,
        BLACKBOX_ENGINE_META_HARNESS,
        BLACKBOX_ENGINE_AUTOSADDLER,
        BLACKBOX_ENGINE_SHINKA_EVOLVE,
    }
)
# Single-mode engines that honor an explicit iteration cap.
BLACKBOX_ITERATION_LIMIT_ENGINES = frozenset(
    {BLACKBOX_ENGINE_META_HARNESS, BLACKBOX_ENGINE_AUTOSADDLER, BLACKBOX_ENGINE_SHINKA_EVOLVE}
)
CLAUDE_CODE_RETIRED = "Claude Code is no longer available as an agent harness. Choose another one, such as codex."
# Extra optimization models a single run may add beyond the reflection model.
BLACKBOX_MAX_EXTRA_REFLECTION_MODELS = 4
# Stands in for ``module_name`` in the job overview and notifications, where
# DSPy jobs record the program they optimized.
BLACKBOX_MODULE_NAME = "blackbox"

# A text artifact under optimization. ``dict`` form names several parts that
# are optimized together (the pinned GEPA engine only).
BlackboxCandidate = str | dict[str, str]


# How a version is scored. ``python`` runs ``metric_code`` inside the managed
# sandbox; ``remote`` POSTs the version and case to ``url`` through the trusted
# parent relay with the
# shared ``secret`` as a bearer token (TODO-1: allow-list + SSRF guard).
# ``model`` is the model a python scorer may call through the injected
# ``llm(prompt, input=None)`` helper (e.g. to run the prompt under
# optimization on a case); its usage is billed with the run.
# ``install_command`` runs inside the offline managed sandbox. It may use
# dependencies already in the immutable image or deployment-owned package
# artifacts; it cannot reach a public package registry at run time.
class BlackboxScorer(BaseModel):
    kind: Literal["python", "remote"] = "python"
    metric_code: str | None = None
    url: str | None = None
    secret: str | None = None
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    install_command: str | None = None
    dependency_lock: ScorerDependencyLock | None = None
    model: ModelConfig | None = None

    @model_validator(mode="after")
    def _ensure_kind_fields(self) -> BlackboxScorer:
        """Require the field the chosen scorer kind reads from.

        Returns:
            The validated scorer instance.

        Raises:
            ValueError: When a python scorer has no code or a remote scorer
                has no URL.
        """
        if self.kind == "python" and not (self.metric_code or "").strip():
            raise ValueError("A python scorer needs metric_code.")
        if self.kind == "remote" and not (self.url or "").strip():
            raise ValueError("A remote scorer needs a url.")
        return self


# Hard stops for a run. ``max_scorer_runs`` caps optimizer-driven scorer
# calls (the final run that re-scores the starting point and the winner is
# outside the cap);
# ``max_iterations`` caps proposer rounds for the engines that iterate
# (Meta-Harness, AutoSaddler, ShinkaEvolve generations); ``stop_at_score`` ends the run early once a version
# reaches it.
class BlackboxBudget(BaseModel):
    max_scorer_runs: int = Field(default=200, ge=1, le=100_000)
    max_iterations: int | None = Field(default=None, ge=1, le=1_000)
    stop_at_score: float | None = None


REPO_SECRET_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"
REPO_NAME_PATTERN = r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
# GitLab nests projects in subgroups (``group/sub/project``); GitHub and Bitbucket use two parts.
REPO_PATH_PATTERN = r"^[A-Za-z0-9_.-]{1,255}(/[A-Za-z0-9_.-]{1,255}){1,20}$"
REPO_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
# An exact host, or ``*.`` and a suffix of two or more labels: ``*.com``
# would let a secret go to any host of a top-level domain.
REPO_SECRET_HOST_PATTERN = re.compile(r"^(\*\.)?[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+$")


# One environment variable a repository needs to build or score. ``value`` is
# entered for this run only; ``saved_secret_id`` points at a secret saved on
# the account. Exactly one is set. Values never reach the job payload: the
# trusted parent vaults them at submission, leaving the opaque
# ``credential_ref``/``credential_revision`` pair in their place, and injects
# them only into the scorer's sandbox, never into an agent's prompt or logs.
class BlackboxRepoSecret(BaseModel):
    name: str = Field(pattern=REPO_SECRET_NAME_PATTERN)
    value: str | None = Field(default=None, max_length=16_384)
    saved_secret_id: str | None = Field(default=None, min_length=1, max_length=64)
    credential_ref: str | None = Field(default=None, min_length=1, max_length=64)
    credential_revision: int | None = Field(default=None, ge=1)
    # The hosts this secret may go to (SKY-381 E52): an exact host or
    # ``*.suffix``. The run, not the repository, sets them, so a shared file
    # cannot send the secret to another host. Empty means no host, and the
    # setup check fails for a user-bound secret that a url uses.
    allowed_hosts: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("allowed_hosts")
    @classmethod
    def _clean_allowed_hosts(cls, hosts: list[str]) -> list[str]:
        """Normalize each allowed host and refuse one that is not a host or ``*.suffix``.

        Args:
            hosts: The hosts as given.

        Returns:
            The lower-case hosts without a trailing dot, in order, without repeats.

        Raises:
            ValueError: When an entry is not an exact host or ``*.`` and a
                suffix of two or more labels.
        """
        cleaned = [host.strip().rstrip(".").lower() for host in hosts]
        for host in cleaned:
            if len(host) > 253 or not REPO_SECRET_HOST_PATTERN.match(host):
                raise ValueError(f"Allowed host '{host}' must be a host such as api.example.com, or *.example.com.")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def _ensure_one_source(self) -> BlackboxRepoSecret:
        """Require exactly one of an inline value or a saved secret.

        Returns:
            The validated secret instance.

        Raises:
            ValueError: When both or neither source is given.
        """
        sources = [self.value is not None, self.saved_secret_id is not None, self.credential_ref is not None]
        if sum(sources) != 1:
            raise ValueError(f"Secret '{self.name}' needs either a value or a saved secret, not both.")
        return self


# The repository a ``repo`` target optimizes. ``commit`` pins the starting
# point; the server resolves it from ``branch`` when omitted, so every version
# is a patch against one fixed tree. ``editable_paths`` are the files and
# folders (relative, ``/``-separated) an agent may change; the rest of the
# repository is read-only context; ``.`` makes the whole repository editable.
# Submodules and Git LFS files are fetched
# read-only. ``created_by_skynet`` marks a repository Skynet created for the
# run: its finished run pushes every version to its own branch and moves
# ``skynet/best`` to the winner, instead of opening a pull request.
class BlackboxRepoSource(BaseModel):
    provider: Literal["github", "gitlab", "bitbucket"] = "github"
    repository: str = Field(pattern=REPO_PATH_PATTERN, max_length=1024)
    branch: str | None = Field(default=None, min_length=1, max_length=255)
    commit: str | None = Field(default=None, pattern=REPO_COMMIT_PATTERN)
    editable_paths: list[str] = Field(min_length=1, max_length=200)
    secrets: list[BlackboxRepoSecret] = Field(default_factory=list, max_length=100)
    # How each ``${NAME}`` in optimize.toml and the role MCP files is filled
    # (SKY-381 E50): ``user`` from the run's secrets (BYOK, no Skynet fee), or
    # ``managed`` with Skynet's key for a catalog host, metered per call. A
    # name this map does not hold binds to ``user``.
    secret_bindings: dict[str, Literal["user", "managed"]] = Field(default_factory=dict, max_length=100)
    created_by_skynet: bool = False

    @model_validator(mode="after")
    def _ensure_clean_paths(self) -> BlackboxRepoSource:
        """Normalize editable paths and reject ones that escape the repository.

        Returns:
            The validated source with ``/``-joined, slash-trimmed paths.

        Raises:
            ValueError: When a path is absolute, empty, or walks out with ``..``,
                when two secrets share a name, when a binding name is invalid,
                or when the repository path does not fit its host.
        """
        if self.provider != "gitlab" and not re.fullmatch(REPO_NAME_PATTERN, self.repository):
            raise ValueError("The repository must be written as 'owner/name'.")
        if self.created_by_skynet and self.provider != "github":
            raise ValueError("Skynet creates new repositories only on GitHub.")
        cleaned: list[str] = []
        for raw in self.editable_paths:
            path = raw.strip().replace("\\", "/")
            if path in (".", "./"):
                cleaned.append(".")
                continue
            if path.startswith("/") or not path.strip("/"):
                raise ValueError(f"Editable path '{raw}' must be relative and stay inside the repository.")
            path = path.strip("/")
            if any(part in ("", ".", "..") for part in path.split("/")):
                raise ValueError(f"Editable path '{raw}' must be relative and stay inside the repository.")
            if path == ".git" or path.startswith(".git/"):
                raise ValueError("The .git folder cannot be editable.")
            cleaned.append(path)
        self.editable_paths = list(dict.fromkeys(cleaned))
        names = [secret.name for secret in self.secrets]
        if len(names) != len(set(names)):
            raise ValueError("Each secret name can only be used once.")
        for name in self.secret_bindings:
            if not re.fullmatch(REPO_SECRET_NAME_PATTERN, name):
                raise ValueError(f"The secret binding name '{name[:100]}' must use only letters, digits and '_'.")
        return self


# What the versions under optimization drive. ``text``: the scorer reads a
# version directly. ``agent``: every scorer run launches a coding harness in a
# private workspace inside the run's managed sandbox, with the version as the
# harness's instruction file(s), and the scorer judges the run record (what
# the agent produced) instead of the version. The ``(harness, model)`` pair is fixed
# for the whole run — the optimizer searches harness text only (joint
# harness + model search is a TODO). ``model`` is the target/worker model
# id as the agent gateway knows it; the optimizer model is
# ``reflection_model_config`` on the request. Clients also send the full
# target role as ``task_model_config`` on the request so its credential source
# can be metered independently; ``model`` remains for stored-client compatibility.
# ``repo``: every version is a patch against ``repo.commit``. The scorer runs
# on a checkout with the patch applied, after ``setup_command``; the coding
# agent that writes versions is the request's ``proposer``. The wizard never
# sends a ``setup_command`` for a repository: the parent infers one from the
# fetched tree while staging it and stores it back on the run.
class BlackboxTarget(BaseModel):
    kind: Literal["text", "agent", "repo"] = BLACKBOX_TARGET_TEXT
    harness: str = BLACKBOX_HARNESS_PI
    model: str | None = None
    timeout_seconds: float = Field(default=600.0, gt=0, le=2_700)
    concurrency: int = Field(default=2, ge=1, le=8)
    setup_command: str | None = None
    install_command: str | None = None
    run_command: str | None = None
    repo: BlackboxRepoSource | None = None

    @model_validator(mode="after")
    def _ensure_agent_fields(self) -> BlackboxTarget:
        """Require what an agent or repository target needs to launch.

        Returns:
            The validated target instance.

        Raises:
            ValueError: When an agent target names no model or an unknown or
                retired harness, a custom harness has no run command, or the
                repository and target kind disagree.
        """
        if (self.kind == BLACKBOX_TARGET_REPO) != (self.repo is not None):
            raise ValueError("A repository target needs a repo, and only a repository target takes one.")
        if self.kind != BLACKBOX_TARGET_AGENT:
            return self
        if not (self.model or "").strip():
            raise ValueError("An agent target needs a model.")
        if self.harness == BLACKBOX_HARNESS_CLAUDE_CODE:
            raise ValueError(CLAUDE_CODE_RETIRED)
        if self.harness not in BLACKBOX_HARNESSES:
            raise ValueError(f"Unknown harness '{self.harness}'. Known harnesses: {', '.join(BLACKBOX_HARNESSES)}.")
        if self.harness == BLACKBOX_HARNESS_CUSTOM and not (self.run_command or "").strip():
            raise ValueError("A custom harness needs a run_command.")
        return self


# The coding agent that drives a harness-based engine (Meta-Harness,
# AutoResearch, AutoSaddler and the Auto lanes built from them). Any harness
# Skynet offers for agent targets can be the proposer; the engine knobs mirror
# the upstream engine configs and are ignored by engines that lack them.
# ``max_tool_calls`` caps the tool calls one proposer session may make: the
# harness is stopped once it starts one more, and the edits it already made
# stand. It also bounds each ShinkaEvolve agent-editor session. Harnesses whose
# output reports no tool calls as they happen (a custom command) are not capped.
# ``host_cli`` offers only Codex and Claude Code: Droid has no flag that skips
# the user's own config, hooks and MCP servers, so it cannot run isolated.
class BlackboxProposer(BaseModel):
    harness: str = BLACKBOX_HARNESS_CODEX
    install_command: str | None = None
    run_command: str | None = None
    max_candidates_per_iter: int | None = Field(default=None, ge=1, le=8)
    ralph: bool = True
    max_no_eval_seconds: float | None = Field(default=None, gt=0, le=7_200)
    max_tool_calls: int = Field(default=100, ge=1, le=1000)
    host_cli: Literal["codex", "claude"] | None = None
    host_permission: Literal["workspace_only", "full_access"] = "workspace_only"

    _refuse_removed_cli = field_validator("host_cli", mode="before")(refuse_removed_cli)

    @model_validator(mode="after")
    def _ensure_launchable(self) -> BlackboxProposer:
        """Require a known harness and a run command for a custom one.

        Returns:
            The validated proposer instance.

        Raises:
            ValueError: When the harness is unknown or retired, or a custom
                harness has no run command.
        """
        if self.harness == BLACKBOX_HARNESS_CLAUDE_CODE:
            raise ValueError(CLAUDE_CODE_RETIRED)
        if self.harness not in BLACKBOX_HARNESSES:
            raise ValueError(
                f"Unknown proposer harness '{self.harness}'. Known harnesses: {', '.join(BLACKBOX_HARNESSES)}."
            )
        if self.harness == BLACKBOX_HARNESS_CUSTOM and not (self.run_command or "").strip():
            raise ValueError("A custom proposer harness needs a run_command.")
        return self


# ShinkaEvolve's evolution knobs (island model, parent selection, mutation
# mix, archive and inspirations, meta notes, parallelism). Every field defaults
# to the upstream Sakana default, except ``use_text_feedback`` which is on so
# scorer feedback reaches the mutation prompts. ``patch_diff``/``patch_full``/
# ``patch_cross`` are the probabilities of each mutation kind and sum to 1.
# ``novelty`` turns on duplicate rejection: proposals too close (by embedding)
# to an existing version are judged by the first optimization model and
# resampled when they add nothing new. ``editor`` picks how a version is
# written: ``single_call`` is upstream's one model call answering with a
# diff, full rewrite or crossover; ``agent`` runs a Pi coding agent (read,
# search and edit tools only) on the parent's files in a scratch directory,
# routed to the model the bandit picked, and the version is whatever it left
# there. The patch mix is ignored in ``agent`` mode.
class BlackboxShinkaSettings(BaseModel):
    num_islands: int = Field(default=2, ge=1, le=8)
    migration_interval: int = Field(default=10, ge=1, le=100)
    migration_rate: float = Field(default=0.0, ge=0, le=1)
    parent_selection: Literal["weighted", "power_law", "beam_search"] = "weighted"
    parent_selection_lambda: float = Field(default=10.0, ge=0.1, le=100)
    exploitation_alpha: float = Field(default=1.0, ge=0, le=10)
    exploitation_ratio: float = Field(default=0.2, ge=0, le=1)
    num_beams: int = Field(default=5, ge=1, le=20)
    patch_diff: float = Field(default=0.6, ge=0, le=1)
    patch_full: float = Field(default=0.3, ge=0, le=1)
    patch_cross: float = Field(default=0.1, ge=0, le=1)
    max_patch_attempts: int = Field(default=1, ge=1, le=10)
    max_patch_resamples: int = Field(default=3, ge=1, le=10)
    archive_size: int = Field(default=40, ge=1, le=500)
    num_archive_inspirations: int = Field(default=1, ge=0, le=10)
    num_top_k_inspirations: int = Field(default=1, ge=0, le=10)
    elite_selection_ratio: float = Field(default=0.3, ge=0, le=1)
    use_text_feedback: bool = True
    novelty: bool = False
    code_embed_sim_threshold: float = Field(default=0.99, ge=0.5, le=1)
    max_novelty_attempts: int = Field(default=3, ge=1, le=10)
    meta_notes: bool = True
    meta_rec_interval: int = Field(default=10, ge=1, le=100)
    meta_max_recommendations: int = Field(default=5, ge=1, le=20)
    max_parallel_evaluations: int = Field(default=2, ge=1, le=8)
    max_parallel_proposals: int = Field(default=2, ge=1, le=8)
    editor: Literal["single_call", "agent"] = "single_call"

    @model_validator(mode="after")
    def _ensure_consistent(self) -> BlackboxShinkaSettings:
        """Require a mutation mix that sums to 1.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: When the three mutation probabilities do not sum to 1.
        """
        if abs(self.patch_diff + self.patch_full + self.patch_cross - 1.0) > 1e-6:
            raise ValueError("patch_diff, patch_full and patch_cross must sum to 1.")
        return self


# ``auto`` explores every available engine on a budget slice, then continues
# from the best version with GEPA; ``single`` runs one named engine.
class BlackboxStrategy(BaseModel):
    mode: Literal["auto", "single"] = "auto"
    engine: str | None = None

    @model_validator(mode="after")
    def _ensure_engine_for_single(self) -> BlackboxStrategy:
        """Require an engine id when a single engine is requested.

        Returns:
            The validated strategy instance.

        Raises:
            ValueError: When ``mode`` is ``single`` and no engine is named.
        """
        if self.mode == "single" and not (self.engine or "").strip():
            raise ValueError("strategy.engine is required when mode is 'single'.")
        return self


# Submission payload for ``POST /blackbox/run``. Not a subclass of the DSPy
# request base: there is no module, signature or column mapping here.
class BlackboxRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    description: str | None = Field(default=None, max_length=280)
    username: str | None = None
    objective: str | None = None
    background: str | None = None
    # Marks a repository run. Engines ignore it; cloning reads it back.
    recipe: Literal["repo"] | None = None
    seed_candidate: BlackboxCandidate | None = None
    scorer: BlackboxScorer
    cases: list[dict[str, Any]] | None = Field(default=None, max_length=200_000)
    # By-reference twin of ``cases`` for agent callers: a dataset already staged
    # server-side, so the rows never travel through the model's tool arguments.
    staged_dataset_id: str | None = Field(default=None, min_length=1, max_length=64)
    # How staged rows map to cases, as in the unified run's data block.
    input_fields: list[str] = Field(default_factory=list)
    expected_field: str | None = None
    seed: int | None = None
    budget: BlackboxBudget = Field(default_factory=BlackboxBudget)
    strategy: BlackboxStrategy = Field(default_factory=BlackboxStrategy)
    target: BlackboxTarget = Field(default_factory=BlackboxTarget)
    proposer_runtime: Literal["vercel", "docker"] = "vercel"
    proposer: BlackboxProposer = Field(default_factory=BlackboxProposer)
    task_model_settings: ModelConfig | None = Field(default=None, alias="task_model_config")
    reflection_model_settings: ModelConfig = Field(alias="reflection_model_config")
    # More optimization models for ShinkaEvolve (alone or as an Auto lane),
    # which picks between all of them, the reflection model first, with a
    # bandit. Other engines ignore them.
    extra_reflection_model_settings: list[ModelConfig] = Field(
        default_factory=list,
        alias="extra_reflection_model_configs",
        max_length=BLACKBOX_MAX_EXTRA_REFLECTION_MODELS,
    )
    shinka: BlackboxShinkaSettings | None = None
    token_source: Literal["managed", "byok", "subscription"] = "managed"
    is_private: bool = False
    economy_mode: bool = Field(
        default=False,
        description=(
            "Send the run's managed chat model calls through OpenRouter's Batch API at half the "
            "token price. Each round of calls waits for its batch, typically minutes and at most a "
            "day, so the run finishes much later. Coding-agent proposer calls and calls on the "
            "user's own OpenRouter key run normally."
        ),
    )
    # The setup's metric-hardening job (SKY-356). A finished job of the same
    # owner ships its exploit suite and counts in the repository run's PR.
    hardening_job_id: str | None = Field(default=None, min_length=1, max_length=36)
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

    @model_validator(mode="before")
    @classmethod
    def _normalize_proposer_runtime(cls, data: Any) -> Any:
        """Map the retired worker selection onto the managed sandbox.

        Args:
            data: Raw request data before field validation.

        Returns:
            A copied mapping with the canonical Vercel runtime when the
            retired worker value was supplied, otherwise the input unchanged.
        """
        if isinstance(data, dict) and data.get("proposer_runtime") == "worker":
            return {**data, "proposer_runtime": "vercel"}
        return data

    @model_validator(mode="after")
    def _ensure_starting_point(self) -> BlackboxRunRequest:
        """Reject starting points and limits the selected engine cannot honor.

        Returns:
            The validated request instance.

        Raises:
            ValueError: When the seed is blank or an empty dict; when a multi-part seed is paired with
                an engine that only takes text; or when an iteration cap is
                supplied outside a single Meta-Harness, AutoSaddler or
                ShinkaEvolve run; or when extra optimization models are sent
                to a single run of another engine.
        """
        seed = self.seed_candidate
        if self.target.kind == BLACKBOX_TARGET_REPO:
            self._ensure_repo_run()
        elif seed is None:
            # The scorer alone drives the climb; with nothing to draft a seed
            # from, the run starts from empty text.
            if not (self.objective or "").strip():
                self.seed_candidate = ""
        elif isinstance(seed, dict):
            if not seed:
                raise ValueError("A multi-part starting point needs at least one part.")
            if self.strategy.mode != "single" or self.strategy.engine not in BLACKBOX_MULTI_PART_ENGINES:
                raise ValueError(
                    "Multi-part starting points are only supported by the "
                    f"{', '.join(sorted(BLACKBOX_MULTI_PART_ENGINES))} engines."
                )
        elif not seed.strip():
            raise ValueError("The starting point cannot be blank.")
        if self.target.kind == BLACKBOX_TARGET_AGENT and self.task_model_settings is not None:
            task_model = self.task_model_settings.normalized_identifier()
            if not task_model:
                raise ValueError("An agent target needs a task model.")
            if self.target.model and self.target.model.strip("/") != task_model:
                raise ValueError("target.model and task_model_config.name must identify the same model.")
            self.target.model = self.task_model_settings.name
        if self.target.kind != BLACKBOX_TARGET_AGENT and self.task_model_settings is not None:
            raise ValueError("task_model_config is only used when the evaluated target is an agent.")
        if self.budget.max_iterations is not None and (
            self.strategy.mode != "single" or self.strategy.engine not in BLACKBOX_ITERATION_LIMIT_ENGINES
        ):
            raise ValueError(
                "An iteration limit is only supported by single Meta-Harness, AutoSaddler or ShinkaEvolve runs."
            )
        if (
            self.extra_reflection_model_settings
            and self.strategy.mode == "single"
            and self.strategy.engine != BLACKBOX_ENGINE_SHINKA_EVOLVE
        ):
            raise ValueError("Extra optimization models are only used by ShinkaEvolve and Auto runs.")
        return self

    def _ensure_repo_run(self) -> None:
        """Check the rules a repository target adds to a run.

        The starting point of a repository run is the pinned commit itself,
        so the seed is the empty patch; a supplied seed must be a patch.

        Raises:
            ValueError: When the seed is multi-part, the scorer is not Python
                code, or a single-engine run names an engine that does not
                optimize a repository.
        """
        multi_part = isinstance(self.seed_candidate, dict)
        if multi_part:
            raise ValueError("A repository run starts from its commit; a multi-part starting point does not apply.")
        if self.scorer.kind != "python":
            raise ValueError("A repository run is scored by Python code that receives the checkout's path.")
        self.seed_candidate = self.seed_candidate or ""
        if self.strategy.mode == "single" and self.strategy.engine not in BLACKBOX_REPO_ENGINES:
            raise ValueError("A repository target runs Auto or one repository engine.")


# ``POST /blackbox/scorer/dry-run``: score one version on one case before
# submitting, so a broken scorer fails in the wizard rather than in the job.
class ScorerDryRunRequest(BaseModel):
    scorer: BlackboxScorer
    candidate: BlackboxCandidate
    case: dict[str, Any] | None = None
    execution_budget_id: str | None = Field(default=None, min_length=1, max_length=64)
    execution_budget_revision: int | None = Field(default=None, ge=1)


class ScorerDryRunResponse(BaseModel):
    ok: bool
    score: float | None = None
    side_info: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    elapsed_ms: float
    usage_by_model: list[ModelTokenUsage] = Field(default_factory=list)
    # What this one check debited, so the wizard can show setup spend against the total budget.
    cents_charged: int = 0
    budget: dict[str, Any] | None = None
    preview_status: Literal["succeeded", "failed", "pending"] | None = None
    preflight_id: str | None = None


# One engine lane of a run. ``explore`` lanes share the budget; the
# ``continue`` lane resumes from the best explore result.
class BlackboxLaneResult(BaseModel):
    engine: str
    phase: Literal["explore", "continue", "single"]
    status: Literal["completed", "failed", "unavailable", "budget_exhausted", "stopped"]
    best_score: float | None = None
    scorer_runs: int = 0
    error: str | None = None


# One distinct version the run scored, in the order versions first appeared.
# ``score`` is the number the run ranked it by: the engine's validation-set
# aggregate when it recorded one (the figure the candidate tree shows), else
# the running mean over its ``evals`` scorer calls, which ``mean_score``
# always carries (``None`` on runs recorded before it existed). ``side_info``
# is what the scorer returned for it last (images as data URLs).
class BlackboxVersion(BaseModel):
    candidate: BlackboxCandidate
    score: float | None = None
    mean_score: float | None = None
    evals: int = 0
    first_run: int = 0
    side_info: dict[str, Any] = Field(default_factory=dict)


# One candidate in the engine's lineage: ``parents`` are indices into the
# same list (``None`` marks the seed), ``val_score`` is the mean validation
# score and ``discovery_evals`` the metric-call count when it appeared.
# Only the GEPA engine records lineage today.
class BlackboxCandidateNode(BaseModel):
    candidate: BlackboxCandidate
    parents: list[int | None] = Field(default_factory=list)
    val_score: float | None = None
    discovery_evals: int = 0


# One named score a scorer returned, with the feedback that explains it.
class BlackboxNamedScore(BaseModel):
    score: float
    feedback: str = ""


# The final run's fresh scores of the starting point and the winner on one case.
# ``*_observation`` is the trusted measurement of that version's run on the
# case (time in ms, each model attempt, metered service cost); see
# ``workspace_harness.build_observation``. ``None`` where nothing measured it.
class BlackboxCaseResult(BaseModel):
    index: int
    baseline_score: float | None = None
    best_score: float | None = None
    baseline_feedback: str | None = None
    best_feedback: str | None = None
    baseline_observation: dict[str, Any] | None = None
    best_observation: dict[str, Any] | None = None


# How a unified run's cases were split. Selection (the winner and the
# regression guard) used ``selection_split``; the reported scores were measured
# on ``report_split``. ``held_out`` is true only when the report used test
# cases no optimizer saw.
class UnifiedSplitCounts(BaseModel):
    train: int
    val: int
    test: int
    held_out: bool
    selection_split: Literal["val", "train", "all"]
    report_split: Literal["test", "val", "all"]


# Result persisted for a finished black-box job. The winner is chosen from the
# optimization run's scores; ``baseline_score`` / ``best_score`` come from a
# separate final run that scores the starting point and the winner afresh (on
# every case, or once each without cases). Named scores are means over cases.
class BlackboxRunResponse(BaseModel):
    optimizer_name: str
    strategy_mode: Literal["auto", "single"]
    engine_used: str
    baseline_score: float | None = None
    best_score: float | None = None
    metric_improvement: float | None = None
    baseline_feedback: str | None = None
    best_feedback: str | None = None
    baseline_named_scores: dict[str, BlackboxNamedScore] = Field(default_factory=dict)
    best_named_scores: dict[str, BlackboxNamedScore] = Field(default_factory=dict)
    case_results: list[BlackboxCaseResult] = Field(default_factory=list)
    case_count: int = 0
    final_scorer_runs: int = 0
    seed_candidate: BlackboxCandidate | None = None
    best_candidate: BlackboxCandidate
    regression_guard_applied: bool = False
    lanes: list[BlackboxLaneResult] = Field(default_factory=list)
    versions: list[BlackboxVersion] = Field(default_factory=list)
    # GEPA's evolutionary lineage; empty for engines that record none.
    candidate_tree: list[BlackboxCandidateNode] = Field(default_factory=list)
    total_scorer_runs: int = 0
    runtime_seconds: float
    num_lm_calls: int = 0
    total_tokens: int | None = None
    usage_by_model: list[ModelTokenUsage] = Field(default_factory=list)
    # Reflection-LM timing on the shared LMActivity shape, so the run view
    # renders the same stage matrix as DSPy runs. Only ``reflection`` is
    # populated — black-box engines drive no generation LM.
    lm_activity: LMActivity | None = None
    optimization_metadata: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    # Unified runs only: how cases were split, the selection scores measured
    # on ``split_counts.selection_split`` (``baseline_score``/``best_score``
    # stay the reported scores, on ``split_counts.report_split``), the
    # optimizer Auto resolved to, and a DSPy optimizer's compiled program.
    split_counts: UnifiedSplitCounts | None = None
    baseline_val_score: float | None = None
    best_val_score: float | None = None
    optimizer_resolved: str | None = None
    program_artifact: ProgramArtifact | None = None

    # Results stored before the final run existed carry the DSPy names for
    # their held-out scores; read them as the baseline and best scores.
    @model_validator(mode="before")
    @classmethod
    def _fold_held_out_scores(cls, data: Any) -> Any:
        """Map a stored result's held-out test metrics onto the final-run scores.

        Args:
            data: The raw result mapping.

        Returns:
            The mapping with ``baseline_score`` / ``best_score`` filled from the
            legacy fields when only those exist.
        """
        if not isinstance(data, dict):
            return data
        legacy = {"baseline_test_metric", "optimized_test_metric", "split_counts"}
        # A unified run's split_counts is the current field, not the legacy
        # DSPy SplitCounts; keep it.
        split = data.get("split_counts")
        if isinstance(split, UnifiedSplitCounts) or (isinstance(split, dict) and "selection_split" in split):
            legacy.discard("split_counts")
        if not legacy & data.keys():
            return data
        folded = {key: value for key, value in data.items() if key not in legacy}
        folded.setdefault("baseline_score", data.get("baseline_test_metric"))
        folded.setdefault("best_score", data.get("optimized_test_metric"))
        return folded

    # Results stored by the retired plateau relay strategy stay readable:
    # they open as Auto runs whose hand-off lanes ended by completing.
    @model_validator(mode="before")
    @classmethod
    def _fold_retired_relay(cls, data: Any) -> Any:
        """Map a stored relay result onto the current strategy values.

        Args:
            data: The raw result mapping.

        Returns:
            The mapping with the relay read as Auto and its lanes as explore lanes.
        """
        if not isinstance(data, dict) or data.get("strategy_mode") != "plateau":
            return data
        lanes = [
            {
                **lane,
                "phase": "explore" if lane.get("phase") == "relay" else lane.get("phase"),
                "status": "completed" if lane.get("status") == "plateaued" else lane.get("status"),
            }
            if isinstance(lane, dict)
            else lane
            for lane in data.get("lanes") or []
        ]
        return {**data, "strategy_mode": "auto", "lanes": lanes}


# One sandboxed agent run of a black-box job, as ``GET
# /optimizations/{id}/agent-runs/{run_id}`` serves it. ``transcript`` starts
# at ``transcript_offset`` so a live viewer fetches only what it lacks.
class BlackboxAgentRunResponse(BaseModel):
    run_id: int
    phase: str
    trial: int | None = None
    example_id: str | None = None
    case_id: str | None = None
    label: str = ""
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    model: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    elapsed_seconds: float | None = None
    error: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    check: dict[str, Any] | None = None
    output: str | None = None
    transcript: str = ""
    transcript_offset: int = 0
    transcript_length: int = 0


# One file or folder of a repository run's tree at its pinned commit;
# ``size`` is a file's byte count when GitHub reports it.
class BlackboxRepoTreeEntry(BaseModel):
    path: str
    type: Literal["file", "dir"]
    size: int | None = None


# ``GET /optimizations/{id}/repository/tree``: every file and folder of a
# repository run at its pinned commit, sorted by path. ``truncated`` is
# GitHub's flag for a tree too large to list whole.
class BlackboxRepoTreeResponse(BaseModel):
    repository: str
    commit: str
    entries: list[BlackboxRepoTreeEntry]
    truncated: bool = False


# ``GET /optimizations/{id}/repository/file``: one file at a repository run's
# pinned commit. ``content`` is set only for a UTF-8 text file within the size
# cap; ``missing`` marks a path the commit lacks, which a version may add.
class BlackboxRepoFileResponse(BaseModel):
    path: str
    content: str | None = None
    binary: bool = False
    too_large: bool = False
    missing: bool = False
    size: int | None = None


# ``POST /optimizations/{id}/repository/archive``: the version to zip, sent as
# its patch text so the request names it exactly as the run recorded it.
class BlackboxRepoArchiveRequest(BaseModel):
    patch: str = Field(max_length=10 * 1024 * 1024)


# One entry of ``GET /blackbox/engines``: the catalog the wizard renders,
# with availability resolved for the requested target kind.
class BlackboxEngineInfo(BaseModel):
    id: str
    label: str
    description: str
    available: bool
    unavailable_reason: str | None = None
    requires_agent_target: bool = False
    supports_parts: bool = False
    checkpoint_recovery_supported: bool = False
    checkpoint_recovery_reason: str | None = None


class SandboxRuntimeCost(BaseModel):
    billing_basis: Literal["at_cost", "included_in_model_markup"]
    minimum_session_cents: str | None = None
    maximum_session_cents: str | None = None
    maximum_lifetime_seconds: float | None = None
    vcpus: int | None = None


class BlackboxProposerRuntimeInfo(BaseModel):
    id: Literal["vercel", "docker"]
    available: bool
    unavailable_reason: str | None = None
    cost: SandboxRuntimeCost
    checkpoint_restore_supported: bool = False
    checkpoint_restore_reason: str | None = None


class BlackboxEngineCatalogResponse(BaseModel):
    target_kind: Literal["text", "agent", "repo"]
    sandbox_available: bool
    sandbox_reason: str | None = None
    engines: list[BlackboxEngineInfo] = Field(default_factory=list)
    # The engines Auto's execution recipe can actually invoke here: a visible
    # catalog entry is not the same as one Auto may run.
    auto_engines: list[str] = Field(default_factory=list)
    auto_available: bool = False
    auto_unavailable_reason: str | None = None
    auto_checkpoint_recovery_supported: bool = False
    auto_checkpoint_recovery_reason: str | None = None
    proposer_runtimes: list[BlackboxProposerRuntimeInfo] = Field(default_factory=list)
    upstream_revision: str | None = None
    run_recovery_eligibility: str = "Requires a supported engine, a compatible saved checkpoint, and funded headroom."

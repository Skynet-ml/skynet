import type { ExecutionBudget, JobExecutionBudget } from "./execution-budget";

export type JobStatus =
  | "pending"
  | "validating"
  | "running"
  | "success"
  | "failed"
  | "cancelled"
  | "paused"
  | "stopped";
export type OptimizationType = "run" | "grid_search" | "blackbox";

// Levels emitted by the backend (`backend/core/api/routers/optimizations_meta.py`).
// `(string & {})` keeps the union behaviour for autocomplete while still
// accepting any backend-future level without a TS error.
type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

// Same brand pattern: documented values plus an escape hatch for any
// backend-future kind (`backend/core/models/dataset.py:42`).
type ProfileKind = "categorical" | "numeric" | "freeform";

export interface ModelConfig {
  name: string;
  /** Billing/auth source for this model only. Older saved configs default to managed. */
  token_source?: "managed" | "byok" | "subscription";
  /** Vault provider slug when a BYOK model comes from a custom connection. */
  byok_provider?: string | null;
  // The CLI whose subscription runs the model; required when token_source is
  // "subscription" (local desktop build only).
  cli_provider?: "codex" | "claude" | null;
  base_url?: string | null;
  temperature?: number | null;
  max_tokens?: number | null;
  // `api_key` is the only well-known extra; the wizard reads/writes it
  // (`features/submit/hooks/use-submit-wizard.ts`). Other keys flow through
  // unchanged.
  extra?: { api_key?: string; [k: string]: unknown };
  /**
   * Wizard state: run this managed model through the provider's batch queue at
   * half price. A submit sends it as `extra.economy_mode` (see prepareModelConfig).
   */
  economy_mode?: boolean;
}

export interface ColumnMapping {
  inputs: Record<string, string>;
  outputs: Record<string, string>;
}

export interface SplitFractions {
  train: number;
  val: number;
  test: number;
}

// React (ReAct-agent) optimization configuration. Mirrors the backend
// `ToolSource` model on legacy DSPy run payloads. React is a generic GEPA module that
// carries a live tool roster; it is otherwise identical to predict/cot, scored
// by the same standard `metric_code`. Only sent when `module_name === "react"`.
export interface ToolSource {
  kind: "live_mcp" | "dataset_snapshot";
  mcp_url?: string | null;
  // Secret bearer/auth header for the MCP endpoint. Never persisted by the
  // backend and never mirrored into shared agent state.
  mcp_auth_header?: string | null;
  tool_filter?: string[] | null;
}

interface SplitCounts {
  train: number;
  val: number;
  test: number;
}

// ---------------------------------------------------------------------------
// Workflow graph wire model — mirrors backend `core/models/workflow.py`.
// A workflow run (`module_name === "workflow"`) carries this spec instead of a
// top-level `signature_code`; per-node signatures live inside the nodes.

interface WorkflowNodePosition {
  x: number;
  y: number;
}

export interface WorkflowFieldSpec {
  name: string;
  // Python type expression ("str", "list[str]", ...). Opaque server-side;
  // the canvas uses it for port coloring.
  annotation?: string;
  description?: string | null;
}

interface WorkflowNodeSpecBase {
  id: string;
  name?: string | null;
  position?: WorkflowNodePosition | null;
}

interface WorkflowInputNodeSpec extends WorkflowNodeSpecBase {
  kind: "input";
  fields: WorkflowFieldSpec[];
}

interface WorkflowOutputNodeSpec extends WorkflowNodeSpecBase {
  kind: "output";
  fields: WorkflowFieldSpec[];
}

export interface WorkflowSignatureNodeSpec extends WorkflowNodeSpecBase {
  kind: "signature";
  module_name: "predict" | "cot" | "react" | "flex";
  signature_code: string;
  /**
   * React and flex nodes only: which tools out of the run-level tool_source the
   * node may call. Null means the full roster on a react node, and no tools at
   * all on a flex node — a Flex is a complete module without them, so it opts in
   * by naming the tools it wants.
   */
  tool_filter?: string[] | null;
}

export interface WorkflowTransformNodeSpec extends WorkflowNodeSpecBase {
  kind: "transform";
  transform_code: string;
  input_fields: WorkflowFieldSpec[];
  output_fields: WorkflowFieldSpec[];
}

interface WorkflowMcpNodeSpec extends WorkflowNodeSpecBase {
  kind: "mcp";
  tool_name: string;
  input_fields: WorkflowFieldSpec[];
  output_field: WorkflowFieldSpec;
}

export type WorkflowNodeSpec =
  | WorkflowInputNodeSpec
  | WorkflowOutputNodeSpec
  | WorkflowSignatureNodeSpec
  | WorkflowTransformNodeSpec
  | WorkflowMcpNodeSpec;

export interface WorkflowEdgeSpec {
  source: string;
  source_port: string;
  target: string;
  target_port: string;
}

export interface WorkflowSpec {
  nodes: WorkflowNodeSpec[];
  edges: WorkflowEdgeSpec[];
}

// One node's execution record from a workflow inference or dry run.
export interface WorkflowNodeTrace {
  node_id: string;
  kind: string;
  name: string;
  inputs: Record<string, unknown>;
  outputs?: Record<string, unknown> | null;
  elapsed_ms: number;
  error?: string | null;
}

export interface WorkflowDryRunRequest {
  workflow: WorkflowSpec;
  inputs: Record<string, unknown>;
  model_config: ModelConfig;
  tool_source?: ToolSource | null;
}

// A node failure is an expected, renderable outcome (200): `error` and
// `failed_node_id` are set and `outputs` is null.
export interface WorkflowDryRunResponse {
  outputs?: Record<string, unknown> | null;
  node_traces: WorkflowNodeTrace[];
  model_used: string;
  error?: string | null;
  failed_node_id?: string | null;
}

export type ExecutionRuntime = "vercel";

export interface RuntimeCostProfile {
  billing_basis: "at_cost" | "included_in_model_markup";
  minimum_session_cents: string | null;
  maximum_session_cents: string | null;
  maximum_lifetime_seconds: number | null;
  vcpus: number | null;
}

export interface OptimizationSubmissionResponse {
  optimization_id: string;
  optimization_type: OptimizationType;
  status: JobStatus;
  created_at: string;
  name?: string | null;
  description?: string | null;
  username: string;
  module_name: string;
  optimizer_name: string;
}

interface RunRecovery {
  state: "recovering" | "recovered" | "unavailable";
  phase?: string | null;
  reason?: string | null;
  execution_generation?: number;
  checkpoint_revision?: string | null;
}

interface TerminalEvidence {
  candidate_origin?: "seed" | "optimized" | null;
  final_evaluation_completed?: boolean;
  final_evaluation_reason?: string | null;
  selection_scope?: "validation" | "training" | "single_task" | "test" | null;
  selection_score?: number | null;
  completed_lanes?: unknown[];
  incumbent?: {
    candidate_id: string;
    candidate_origin: "seed" | "optimized";
    candidate: Record<string, string>;
    selection_score: number;
    selection_scope: "validation" | "training";
    evaluated_examples: number;
    discovered_at_evals: number;
    iteration?: number | null;
  };
  execution_budget?: JobExecutionBudget | null;
  budget_projection?: BudgetProjection | null;
}

/** The worker's measured-burn projection that parked a run at its checkpoint. */
interface BudgetProjection {
  planned_calls: number;
  done_calls: number;
  spent_cents: string;
  projected_cents: number;
  limit_cents: number;
}

export interface OptimizationSummaryResponse {
  optimization_id: string;
  optimization_type: OptimizationType;
  status: JobStatus;
  stop_reason?: string | null;
  result_availability?: "evaluated" | "none" | null;
  terminal_evidence?: TerminalEvidence | null;
  recovery?: RunRecovery | null;
  execution_budget?: JobExecutionBudget | null;
  message?: string | null;
  name?: string | null;
  description?: string | null;
  pinned?: boolean;
  archived?: boolean;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  elapsed?: string | null;
  elapsed_seconds?: number | null;
  estimated_remaining?: string | null;
  username?: string | null;
  module_name?: string | null;
  module_kwargs?: Record<string, unknown>;
  optimizer_name?: string | null;
  column_mapping?: ColumnMapping;
  dataset_rows?: number | null;
  /** Stored footprint of this run in bytes (artifacts + payload + logs), counted against the storage quota; 0 when unmeasured. */
  stored_bytes?: number;
  /** Id of the library dataset this run was submitted from (by-reference); null for inline/staged submits. */
  source_dataset_id?: string | null;
  /** True when this run stopped mid-optimization with a saved checkpoint and can be resumed in place; drives Resume vs Restart. */
  resumable?: boolean;
  /** True while this run is actively running AND already has a saved checkpoint; drives the Pause control. */
  pausable?: boolean;
  latest_metrics?: Record<string, unknown>;
  model_name?: string | null;
  model_settings?: Record<string, unknown>;
  reflection_model_name?: string | null;
  task_model_name?: string | null;
  total_pairs?: number | null;
  completed_pairs?: number | null;
  failed_pairs?: number | null;
  generation_models?: ModelConfig[];
  reflection_models?: ModelConfig[];
  split_fractions?: SplitFractions;
  shuffle?: boolean;
  seed?: number | null;
  optimizer_kwargs?: Record<string, unknown>;
  compile_kwargs?: Record<string, unknown>;
  progress_count?: number | null;
  log_count?: number | null;
  baseline_test_metric?: number | null;
  optimized_test_metric?: number | null;
  metric_improvement?: number | null;
  best_pair_label?: string | null;
  summary_text?: string | null;
  /** Caller's share role when this run was reached via a member grant; null/absent for owned runs. */
  role?: "viewer" | "editor" | "owner" | null;
}

export interface PaginatedJobsResponse {
  items: OptimizationSummaryResponse[];
  total: number;
  limit: number;
  offset: number;
}

/** Where a run-log line came from; sandbox sources are attributed by the host. */
export type LogSource = "host" | "engine" | "proposer" | "scorer" | "sandbox";

export interface OptimizationLogEntry {
  /** Row id: the live stream's resume cursor. Absent on demo data. */
  id?: number | null;
  timestamp: string;
  level: LogLevel | (string & {});
  logger: string;
  message: string;
  pair_index?: number | null;
  source?: LogSource | (string & {});
  /** Typed event name, such as `candidate.scored`, or a scorer's own name. */
  event?: string | null;
  fields?: Record<string, unknown> | null;
  candidate?: string | null;
  case?: string | null;
}

export interface ProgressEvent {
  timestamp: string;
  event?: string | null;
  metrics: Record<string, unknown>;
}

interface OptimizedDemo {
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
}

export interface OptimizedPredictor {
  predictor_name: string;
  signature_name?: string | null;
  instructions: string;
  input_fields: string[];
  output_fields: string[];
  demos: OptimizedDemo[];
  formatted_prompt: string;
}

// Optimized react-agent overlay: the actual artifact a react run produces —
// GEPA-tuned per-tool descriptions, optional renamed display names, and the
// loop budget. Mirrors backend ReactOverlay.
export interface ReactOverlay {
  tool_descriptions: Record<string, string>;
  tool_arg_descriptions: Record<string, Record<string, string>>;
  tool_schema_hashes: Record<string, string>;
  max_iters: number;
  tool_source?: Record<string, unknown> | null;
  /** GEPA-proposed display names, { canonical: proposed }. */
  tool_names?: Record<string, string> | null;
  /**
   * Per-tool approval severity ("info" | "warning" | "destructive") derived
   * from the source MCP's tool annotations, { tool_name: severity }. Only tools
   * whose server stated a hint appear; absent for pre-severity artifacts.
   */
  tool_severities?: Record<string, string>;
}

// The optimized surface of a single workflow node, keyed under its component
// path (`n_<node_id>`) in `ProgramArtifact.optimized_nodes`. A signature node
// carries `optimized_prompt` (and, for react nodes, a `react_overlay`); a flex
// node carries its rewritten `optimized_src`. Mirrors backend NodeArtifact.
export interface NodeArtifact {
  optimized_prompt?: OptimizedPredictor | null;
  react_overlay?: ReactOverlay | null;
  optimized_src?: string | null;
}

interface ProgramArtifact {
  path?: string | null;
  program_state_json?: Record<string, unknown> | null;
  program_pickle_base64?: string | null;
  metadata?: Record<string, unknown>;
  optimized_prompt?: OptimizedPredictor;
  react_overlay?: ReactOverlay | null;
  /**
   * GEPA-rewritten module source for a dspy.Flex program — the optimized Python
   * that runs in the serve sandbox. Absent for non-Flex artifacts, whose
   * optimization lands in the prompt rather than the code.
   */
  optimized_module_src?: string | null;
  /**
   * GEPA-rewritten source per Flex submodule, keyed by its component path (a
   * workflow's flex node is `n_<node_id>`). Empty unless the program nests Flex
   * modules rather than being one itself.
   */
  optimized_component_srcs?: Record<string, string>;
  /**
   * Per-node optimized surface for a workflow program, keyed by component path
   * (`n_<node_id>`): each node's prompt, react overlay, or rewritten code. Empty
   * for scalar (single-module) programs.
   */
  optimized_nodes?: Record<string, NodeArtifact>;
}

export interface EvalExampleResult {
  index: number;
  outputs: Record<string, unknown>;
  score: number;
  pass: boolean;
  error?: string | null;
  // Named scores the metric logged via log_metrics while scoring this row.
  logged_metrics?: Record<string, number>;
}

export interface LMStageStats {
  calls: number;
  avg_response_time_ms?: number | null;
}

export interface LMActivity {
  generation: Record<string, LMStageStats>;
  reflection: Record<string, LMStageStats>;
}

/**
 * What a finished run cost against the balance ledger: every run bills, and
 * `cents` is the charged amount. Stamped by the worker under
 * `RunResult.details.billing`.
 */
export interface RunBillingOutcome {
  outcome: "billed";
  cents: number;
  // The projected cost bracket persisted at submit, echoed back so the
  // estimate can be reconciled against the actual charge. Absent on runs
  // submitted before an estimate was persisted (older runs).
  estimated_low?: number;
  estimated_high?: number;
}

export interface PairResult {
  pair_index: number;
  generation_model: string;
  reflection_model: string;
  generation_reasoning_effort?: string | null;
  reflection_reasoning_effort?: string | null;
  baseline_test_metric?: number | null;
  optimized_test_metric?: number | null;
  metric_improvement?: number | null;
  runtime_seconds?: number | null;
  num_lm_calls?: number | null;
  avg_response_time_ms?: number | null;
  lm_activity?: LMActivity | null;
  program_artifact?: ProgramArtifact | null;
  error?: string | null;
  baseline_test_results?: EvalExampleResult[];
  optimized_test_results?: EvalExampleResult[];
  baseline_logged_metrics?: Record<string, number>;
  optimized_logged_metrics?: Record<string, number>;
}

interface RunResult {
  module_name: string;
  optimizer_name: string;
  metric_name?: string | null;
  split_counts?: SplitCounts;
  baseline_test_metric?: number | null;
  optimized_test_metric?: number | null;
  metric_improvement?: number | null;
  optimization_metadata?: Record<string, unknown>;
  details?: Record<string, unknown>;
  program_artifact_path?: string | null;
  program_artifact?: ProgramArtifact | null;
  runtime_seconds?: number | null;
  num_lm_calls?: number | null;
  avg_response_time_ms?: number | null;
  lm_activity?: LMActivity | null;
  run_log?: OptimizationLogEntry[];
  baseline_test_results?: EvalExampleResult[];
  optimized_test_results?: EvalExampleResult[];
  // log_metrics aggregates: each name macro-averaged over the test rows that
  // logged it, for the baseline and optimized evaluations respectively.
  baseline_logged_metrics?: Record<string, number>;
  optimized_logged_metrics?: Record<string, number>;
}

export interface GridSearchResult {
  module_name: string;
  optimizer_name: string;
  metric_name?: string | null;
  split_counts?: SplitCounts;
  total_pairs: number;
  completed_pairs: number;
  failed_pairs: number;
  pair_results: PairResult[];
  best_pair?: PairResult | null;
  runtime_seconds?: number | null;
}

// ── Black-box ("Optimize Anything") ─────────────────────────────────────────
// Mirrors `backend/core/models/blackbox.py`. Scores are raw floats on whatever
// scale the user's scorer returns — never percentages.

/** A candidate is one text, or a dict of named parts (GEPA / meta_harness only). */
export type BlackboxCandidate = string | Record<string, string>;

export type BlackboxEngineId =
  | "gepa"
  | "best_of_n"
  | "autoresearch"
  | "meta_harness"
  | "autosaddler"
  | "shinka_evolve";
export type BlackboxHarness = "pi" | "codex" | "opencode" | "prime" | "custom";
export type BlackboxProposerRuntime = "vercel";

export interface ScorerDependencyLock {
  code_sha256: string;
  requirements: string[];
  inferred: string[];
  imports: string[];
  artifacts: Array<{
    name: string;
    version: string;
    filename: string;
    url: string;
    sha256: string;
  }>;
  python: string;
  image: string;
  registry_url: string;
  signature: string;
}

export interface BlackboxScorer {
  kind: "python" | "remote";
  metric_code?: string | null;
  url?: string | null;
  secret?: string | null;
  timeout_seconds?: number;
  // Runs once when a python scorer's sandbox opens — apt-get or pip for what
  // the scorer imports. Null when the stock box already has everything.
  install_command?: string | null;
  dependency_lock?: ScorerDependencyLock | null;
  // The model injected into the python scorer as `llm()` — the "model that
  // runs your prompt". Null when the scorer never calls a model.
  model?: ModelConfig | null;
}

export interface BlackboxBudget {
  max_scorer_runs: number;
  max_iterations?: number | null;
  stop_at_score?: number | null;
}

/** One environment variable the repository scorer receives: typed in, or saved on the account. */
export interface BlackboxRepoSecret {
  name: string;
  value?: string | null;
  saved_secret_id?: string | null;
}

/** A GitHub repository optimized as a whole; each version is a patch against `commit`. */
export interface BlackboxRepoSource {
  provider?: "github";
  repository: string;
  branch?: string | null;
  commit?: string | null;
  editable_paths: string[];
  secrets?: BlackboxRepoSecret[];
}

export interface BlackboxTarget {
  kind: "text" | "agent" | "repo";
  repo?: BlackboxRepoSource | null;
  harness?: BlackboxHarness;
  model?: string | null;
  timeout_seconds?: number;
  concurrency?: number;
  setup_command?: string | null;
  install_command?: string | null;
  run_command?: string | null;
}

export interface BlackboxStrategy {
  mode: "auto" | "single";
  engine?: BlackboxEngineId | null;
}

/** The coding agent that drives a harness-based engine's optimization loop. */
export interface BlackboxProposer {
  harness: BlackboxHarness;
  install_command?: string | null;
  run_command?: string | null;
  max_candidates_per_iter?: number | null;
  ralph?: boolean;
  max_no_eval_seconds?: number | null;
  // Caps tool calls per candidate for every coding-agent proposer, and for
  // ShinkaEvolve's agent editor.
  max_tool_calls?: number | null;
  // Local desktop build only: run the proposer as this signed-in CLI on the
  // host, confined to the run workspace unless the user grants full access.
  host_cli?: "codex" | "claude" | null;
  host_permission?: "workspace_only" | "full_access";
}

export type BlackboxShinkaParentSelection = "weighted" | "power_law" | "beam_search";

/** How ShinkaEvolve writes a version: one model call, or the Pi coding agent with file tools. */
export type BlackboxShinkaEditor = "single_call" | "agent";

/** ShinkaEvolve's search settings; a request without the block runs every default. */
export interface BlackboxShinkaSettings {
  editor: BlackboxShinkaEditor;
  num_islands: number;
  migration_interval: number;
  migration_rate: number;
  parent_selection: BlackboxShinkaParentSelection;
  parent_selection_lambda: number;
  exploitation_alpha: number;
  exploitation_ratio: number;
  num_beams: number;
  patch_diff: number;
  patch_full: number;
  patch_cross: number;
  max_patch_attempts: number;
  max_patch_resamples: number;
  archive_size: number;
  num_archive_inspirations: number;
  num_top_k_inspirations: number;
  elite_selection_ratio: number;
  use_text_feedback: boolean;
  novelty: boolean;
  code_embed_sim_threshold: number;
  max_novelty_attempts: number;
  meta_notes: boolean;
  meta_rec_interval: number;
  meta_max_recommendations: number;
  max_parallel_evaluations: number;
  max_parallel_proposals: number;
}

export interface BlackboxRunRequest {
  execution_budget_id?: string;
  execution_budget_revision?: number;
  preflight_id?: string;
  preflight_fingerprint?: string;

  name?: string;
  description?: string;
  username?: string;
  objective?: string | null;
  background?: string | null;
  // Wizard recipe that authored the run; cloning preselects the picker with it.
  recipe?: "repo" | null;
  seed_candidate?: BlackboxCandidate | null;
  scorer: BlackboxScorer;
  cases?: Array<Record<string, unknown>> | null;
  staged_dataset_id?: string | null;
  input_fields?: string[];
  expected_field?: string | null;
  seed?: number | null;
  budget: BlackboxBudget;
  strategy: BlackboxStrategy;
  proposer_runtime?: BlackboxProposerRuntime;
  proposer?: BlackboxProposer;
  target: BlackboxTarget;
  task_model_config?: ModelConfig | null;
  reflection_model_config: ModelConfig;
  // More optimization models for ShinkaEvolve, which learns which one
  // improves scores most; every other engine ignores them.
  extra_reflection_model_configs?: ModelConfig[];
  shinka?: BlackboxShinkaSettings | null;
  token_source?: "managed" | "byok" | "subscription";
  is_private?: boolean;
  // Batch managed chat model calls at half price; coding-agent proposer calls
  // and BYOK calls run normally.
  economy_mode?: boolean;
  max_cost_cents?: number | null;
  estimated_cents_low?: number | null;
  estimated_cents_high?: number | null;
}

export interface ScorerDryRunRequest {
  scorer: BlackboxScorer;
  candidate: BlackboxCandidate;
  case?: Record<string, unknown> | null;
}

export interface ModelTokenUsage {
  model: string;
  input_tokens: number;
  output_tokens: number;
}

export interface ScorerDryRunResponse {
  ok: boolean;
  score?: number | null;
  side_info: Record<string, unknown>;
  error?: string | null;
  elapsed_ms: number;
  // Per-model token usage when the scorer called the injected `llm()` helper.
  usage_by_model?: ModelTokenUsage[];
  // Optional per-check attribution; the shared budget owns cumulative setup spending.
  cents_charged?: number;
}

interface BlackboxLaneResult {
  engine: BlackboxEngineId;
  phase: "explore" | "continue" | "single";
  status: "completed" | "failed" | "unavailable" | "budget_exhausted";
  best_score?: number | null;
  scorer_runs: number;
  error?: string | null;
}

/**
 * One distinct version the run scored, in first-seen order. `side_info` is
 * what the scorer returned for it last; images arrive as data URLs.
 */
export interface BlackboxVersion {
  candidate: BlackboxCandidate;
  /** The score the run ranked it by: the validation-set aggregate when the engine recorded one, else the running mean. */
  score?: number | null;
  /** Running mean over its `evals` scorer calls; absent on runs recorded before it existed. */
  mean_score?: number | null;
  evals: number;
  first_run: number;
  side_info: Record<string, unknown>;
}

/**
 * One candidate in the engine's lineage: `parents` are indices into the same
 * list (`null` marks the seed). Only the GEPA engine records lineage today.
 */
interface BlackboxCandidateNode {
  candidate: BlackboxCandidate;
  parents: Array<number | null>;
  val_score?: number | null;
  discovery_evals: number;
}

/** One named score a scorer returned, with its own feedback. */
export interface BlackboxNamedScore {
  score: number;
  feedback: string;
}

/** One case of the final run: the starting version and the winner, scored afresh. */
export interface BlackboxCaseResult {
  index: number;
  baseline_score?: number | null;
  best_score?: number | null;
  baseline_feedback?: string | null;
  best_feedback?: string | null;
  /** The trusted measurement of each version's run on the case (time, model
   *  attempts, metered service cost); absent where nothing measured it. */
  baseline_observation?: BlackboxObservation | null;
  best_observation?: BlackboxObservation | null;
}

/** One response's trusted observation; see `workspace_harness.build_observation`. */
export interface BlackboxObservation {
  timing?: { task_ms?: number | null; [key: string]: unknown } | null;
  cost?: {
    usd?: number | null;
    status?: string | null;
    reason?: string | null;
    [key: string]: unknown;
  } | null;
  [key: string]: unknown;
}

export interface BlackboxRunResult {
  optimizer_name: string;
  strategy_mode: "auto" | "single";
  engine_used: BlackboxEngineId;
  /** Fresh final-run score of the starting version. */
  baseline_score?: number | null;
  /** Fresh final-run score of the winner. */
  best_score?: number | null;
  metric_improvement?: number | null;
  /** The final run's top-level feedback; null when there are cases (see `case_results`). */
  baseline_feedback?: string | null;
  best_feedback?: string | null;
  /** Named scores from the final run, averaged over cases; absent on older runs. */
  baseline_named_scores?: Record<string, BlackboxNamedScore>;
  best_named_scores?: Record<string, BlackboxNamedScore>;
  /** One row per case of the final run; empty without cases. */
  case_results?: BlackboxCaseResult[];
  case_count?: number;
  /** Fresh scorer runs spent on the final run, outside `max_scorer_runs`. */
  final_scorer_runs?: number;
  /** Runs recorded before the final run existed. Unified runs store a
   *  `UnifiedSplitCounts` object here instead. */
  split_counts?: Record<string, number>;
  /** Unified runs: selection scores on `split_counts.selection_split`. */
  baseline_val_score?: number | null;
  best_val_score?: number | null;
  /** Unified runs: the optimizer Auto resolved to. */
  optimizer_resolved?: string | null;
  baseline_test_metric?: number | null;
  optimized_test_metric?: number | null;
  seed_candidate?: BlackboxCandidate | null;
  best_candidate: BlackboxCandidate;
  regression_guard_applied: boolean;
  lanes: BlackboxLaneResult[];
  /** Absent on runs recorded before version tracking existed. */
  versions?: BlackboxVersion[];
  /** GEPA's evolutionary lineage; absent or empty for other engines and older runs. */
  candidate_tree?: BlackboxCandidateNode[];
  total_scorer_runs: number;
  runtime_seconds: number;
  num_lm_calls: number;
  total_tokens?: number | null;
  usage_by_model: Array<Record<string, unknown>>;
  /** Reflection-model stage timing; absent on runs recorded before it existed. */
  lm_activity?: LMActivity | null;
  optimization_metadata: Record<string, unknown>;
  details: Record<string, unknown>;
  /** Unified runs on a DSPy optimizer: the compiled program. */
  program_artifact?: ProgramArtifact | null;
}

export interface RepositoryTreeEntry {
  path: string;
  type: "file" | "dir";
  size: number | null;
}

/** A repository run's tree at its pinned commit. */
export interface RepositoryTreeResponse {
  repository: string;
  commit: string;
  entries: RepositoryTreeEntry[];
  /** True when the listing stops short of the whole tree. */
  truncated: boolean;
}

/** One file of a repository run at its pinned commit. */
export interface RepositoryFileResponse {
  path: string;
  /** Null when the file is binary, too large, or missing. */
  content: string | null;
  binary: boolean;
  too_large: boolean;
  /** Not in the pinned commit — normal for a file a version adds. */
  missing: boolean;
  size: number | null;
}

export interface BlackboxEngineInfo {
  id: BlackboxEngineId;
  label: string;
  description: string;
  available: boolean;
  unavailable_reason?: string | null;
  requires_agent_target: boolean;
  supports_parts: boolean;
  checkpoint_recovery_supported: boolean;
  checkpoint_recovery_reason: string | null;
}

export interface BlackboxEngineCatalogResponse {
  target_kind: "text" | "agent" | "repo";
  sandbox_available: boolean;
  sandbox_reason?: string | null;
  engines: BlackboxEngineInfo[];
  auto_engines: BlackboxEngineId[];
  auto_available: boolean;
  auto_unavailable_reason: string | null;
  auto_checkpoint_recovery_supported: boolean;
  auto_checkpoint_recovery_reason: string | null;
  upstream_revision: string;
  run_recovery_eligibility: string;
  proposer_runtimes: Array<{
    id: BlackboxProposerRuntime;
    available: boolean;
    unavailable_reason: string | null;
    cost: RuntimeCostProfile;
    checkpoint_restore_supported: boolean;
    checkpoint_restore_reason: string | null;
  }>;
}

/** One sandboxed agent run of a black-box optimization, with a transcript tail. */
export interface BlackboxAgentRunResponse {
  run_id: number;
  phase: string;
  trial?: number | null;
  example_id?: string | null;
  case_id?: string | null;
  label: string;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  /** The model the harness drove in this run. */
  model?: string | null;
  exit_code?: number | null;
  timed_out: boolean;
  elapsed_seconds?: number | null;
  error?: string | null;
  usage: Record<string, unknown>;
  check?: Record<string, unknown> | null;
  output?: string | null;
  /** The transcript from ``transcript_offset`` on; the whole of it when the offset is 0. */
  transcript: string;
  transcript_offset: number;
  transcript_length: number;
}

export interface OptimizationStatusResponse extends OptimizationSummaryResponse {
  progress_events: ProgressEvent[];
  logs: OptimizationLogEntry[];
  /**
   * Start index of the `progress_events` / `logs` slices within the full
   * server-side stream. 0 (or absent) means the slice is the complete stream;
   * a positive value marks a delta tail returned for a `since_progress` /
   * `since_log` cursor, to be spliced onto rows already held client-side.
   */
  progress_offset?: number;
  logs_offset?: number;
  result?: RunResult | null;
  grid_result?: GridSearchResult | null;
  blackbox_result?: BlackboxRunResult | null;
  /** Caller's share role when reached via a member grant; null for the owner's own view. */
  effective_role?: "viewer" | "editor" | "owner" | null;
}

export interface ValidateCodeResponse {
  valid: boolean;
  signature_fields?: { inputs: string[]; outputs: string[] };
  errors: string[];
  warnings: string[];
}

export interface ValidateDatasetRequest {
  row_count: number;
  fractions: SplitFractions;
}

export interface ValidateDatasetResponse {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

export interface QueueStatusResponse {
  pending_jobs: number;
  active_jobs: number;
  worker_threads: number;
  workers_alive: boolean;
}

export interface OptimizationPayloadResponse {
  optimization_id: string;
  optimization_type: OptimizationType;
  payload: Record<string, unknown>;
}

interface DatasetRow {
  index: number;
  row: Record<string, unknown>;
}

export interface OptimizationDatasetResponse {
  total_rows: number;
  splits: {
    train: DatasetRow[];
    val: DatasetRow[];
    test: DatasetRow[];
  };
  column_mapping: ColumnMapping;
  split_counts: SplitCounts;
}

export interface ServeInfoResponse {
  optimization_id: string;
  module_name: string;
  optimizer_name: string;
  model_name: string;
  input_fields: string[];
  output_fields: string[];
  instructions?: string | null;
  demo_count: number;
  /** Example input values (from a demo or the dataset) to prefill usage snippets. */
  sample_inputs?: Record<string, string>;
}

export interface ServeResponse {
  optimization_id: string;
  outputs: Record<string, unknown>;
  input_fields: string[];
  output_fields: string[];
  model_used: string;
  // Per-node execution trace, present only for workflow runs.
  node_traces?: WorkflowNodeTrace[] | null;
  cents_charged?: string | null;
  budget?: ExecutionBudget | null;
}

export interface CatalogModel {
  value: string;
  label: string;
  provider: string;
  /** Vault provider slug to persist when this model came from a custom BYOK connection. */
  byok_provider?: string | null;
  data_center?: string | null;
  supports_thinking: boolean;
  // Effort levels the provider accepts for this model, weakest first; empty
  // when it reasons without an effort control, absent/null when unknown.
  reasoning_efforts?: string[] | null;
  default_reasoning_effort?: string | null;
  // Always reasons (rejects effort "none"), and reasons when no effort is
  // sent; null when the provider did not say.
  reasoning_mandatory?: boolean | null;
  reasoning_default_enabled?: boolean | null;
  // A local CLI row whose CLI takes fast mode for this model; absent elsewhere.
  supports_fast?: boolean;
  // Newest headline model of a leading lab, derived from provider metadata.
  featured?: boolean;
  // The best-value default thinker that the composer's Auto mode runs.
  is_default?: boolean;
  // The fast, cheap model every agent interview runs when the user picks
  // nothing; at most one model carries it.
  is_interview_default?: boolean;
  // The featured default thinker with the highest intelligence index; the
  // reflection model's Automatic runs it. At most one model carries it.
  is_strongest?: boolean;
  // OpenRouter's human name without the vendor prefix ("Claude Haiku 5.5");
  // null for models OpenRouter does not list.
  display_name?: string | null;
  supports_vision: boolean;
  // Best-value rank of the model's lab: 0 top labs (Anthropic, OpenAI,
  // Google), 1 others, 2 Chinese labs. Absent reads as 1.
  lab_tier?: number;
  available: boolean;
  max_input_tokens?: number | null;
  // Provider per-token costs (USD) from LiteLLM; null/absent when unpriced, so
  // the estimate falls back to a default rate rather than treating it as free.
  input_cost_per_token?: number | null;
  output_cost_per_token?: number | null;
  // Artificial Analysis intelligence index (a new release may carry its
  // predecessor's); null when unrated.
  intelligence_index?: number | null;
  // The model's Artificial Analysis page, only once the backend has fetched it
  // and confirmed it names this model; null otherwise (no link shown).
  artificial_analysis_url?: string | null;
  // Live OpenRouter output tokens/s of the fastest healthy endpoint; null when unmeasured.
  throughput_tps?: number | null;
  // Median ms to the first token on that endpoint; null when unmeasured.
  ttft_ms?: number | null;
  // Estimated ms to a complete 150-token reply on that endpoint; null when unmeasured.
  latency_ms?: number | null;
  // At least one OpenRouter endpoint serves this model with zero data retention.
  zero_retention?: boolean;
}

/** Which providers may serve the account's platform-paid model calls. */
export type ModelDataPolicy = "allow" | "deny" | "zdr";

export interface ModelPrivacySettings {
  data_policy: ModelDataPolicy;
}

export interface CatalogProvider {
  slug: string;
  label: string;
  data_center?: string | null;
  env_var?: string | null;
  default_base_url?: string | null;
  has_env_key: boolean;
}

export interface ModelCatalogResponse {
  providers: CatalogProvider[];
  models: CatalogModel[];
}

interface TargetColumnProfile {
  name: string;
  kind: ProfileKind | (string & {});
  unique_values: number;
  class_histogram: Record<string, number>;
}

type ColumnKind = "text" | "image";

interface InputColumnProfile {
  name: string;
  kind: ColumnKind;
}

export interface DatasetProfile {
  row_count: number;
  column_count: number;
  target: TargetColumnProfile | null;
  targets: TargetColumnProfile[];
  inputs: InputColumnProfile[];
  duplicate_count: number;
}

export interface SplitPlan {
  fractions: SplitFractions;
  shuffle: boolean;
  seed: number;
  counts: SplitCounts;
  // Black-box engine the fractions were sized for; null/absent is the GEPA-tuned default.
  engine?: BlackboxEngineId | null;
  rationale: string[];
}

export interface ProfileDatasetRequest {
  dataset: Array<Record<string, unknown>>;
  column_mapping: ColumnMapping;
  seed?: number | null;
  engine?: BlackboxEngineId | null;
}

export interface ProfileDatasetResponse {
  profile: DatasetProfile;
  plan: SplitPlan;
}

/**
 * Unified (wizard v2) run contract. Mirrors `backend/core/models/unified.py`
 * field for field (snake_case), plus the pure optimizer rules the backend
 * applies, so the wizard shows the same choices the server will accept.
 */
import type {
  BlackboxBudget,
  BlackboxProposer,
  BlackboxShinkaSettings,
  ModelConfig,
  ScorerDependencyLock,
  SplitFractions,
} from "@/shared/types/api";

export type ArtifactKind = "dspy_program";
/** What the optimizer catalog is keyed on: a unified run's artifact, or a repository run. */
export type OptimizerTarget = ArtifactKind | "repo";
export type DataMode = "single" | "pairs";
export type OptimizerChoice =
  | "auto"
  | "dspy_gepa"
  | "dspy_miprov2"
  | "dspy_bootstrap_fewshot"
  | "gepa"
  | "best_of_n"
  | "autoresearch"
  | "meta_harness"
  | "autosaddler"
  | "shinka_evolve";
export type OptimizerFamily = "auto" | "dspy" | "blackbox";
export type UnifiedCandidate = string | Record<string, string>;

export const UNIFIED_PAYLOAD_KIND = "unified_v1";
export const DSPY_OPTIMIZERS = ["dspy_gepa", "dspy_miprov2", "dspy_bootstrap_fewshot"] as const;
export const BLACKBOX_OPTIMIZERS = [
  "gepa",
  "best_of_n",
  "autoresearch",
  "meta_harness",
  "autosaddler",
  "shinka_evolve",
] as const;
// "auto" counts because both of its resolutions consume feedback text.
const FEEDBACK_OPTIMIZERS: ReadonlySet<string> = new Set(["dspy_gepa", "gepa", "auto"]);
export const MIN_PAIRS_FOR_HELD_OUT = 5;
export const CONTRACT_SAMPLE_MAX = 5;

/**
 * Optimizer choices that fit a target and data mode. A DSPy program lists
 * `"auto"` first; a repository run lists only the black-box engines, as Auto
 * has no repository strategy.
 */
export function optimizersFor(target: OptimizerTarget, mode: DataMode): OptimizerChoice[] {
  if (target === "dspy_program") {
    return mode === "pairs"
      ? ["auto", "dspy_gepa", "dspy_miprov2", "dspy_bootstrap_fewshot"]
      : ["auto", "dspy_gepa"];
  }
  return [...BLACKBOX_OPTIMIZERS];
}

/** The optimizer that actually runs: Auto is DSPy GEPA on a DSPy program, else the black-box Auto strategy. */
export function resolveOptimizer(choice: OptimizerChoice, target: OptimizerTarget): string {
  if (choice === "auto") return target === "dspy_program" ? "dspy_gepa" : "auto";
  return choice;
}

export function optimizerFamily(resolved: string): OptimizerFamily {
  if ((DSPY_OPTIMIZERS as readonly string[]).includes(resolved)) return "dspy";
  if (resolved === "auto") return "auto";
  return "blackbox";
}

/** Whether the resolved optimizer reads the metric's feedback text. */
export function usesFeedback(choice: OptimizerChoice, target: OptimizerTarget): boolean {
  return FEEDBACK_OPTIMIZERS.has(resolveOptimizer(choice, target));
}

export interface UnifiedCase {
  input: Record<string, unknown>;
  expected?: unknown;
}

export interface UnifiedArtifact {
  kind: ArtifactKind;
  starting_version: UnifiedCandidate;
}

export interface UnifiedTask {
  objective: string;
  background?: string;
  artifact: UnifiedArtifact;
}

export interface UnifiedData {
  mode: DataMode;
  cases?: UnifiedCase[] | null;
  staged_dataset_id?: string | null;
  input_fields?: string[];
  expected_field?: string | null;
  split?: SplitFractions;
  shuffle?: boolean;
}

export interface UnifiedOptimizer {
  choice: OptimizerChoice;
  budget: BlackboxBudget;
  proposer?: BlackboxProposer;
  shinka?: BlackboxShinkaSettings | null;
  dspy_kwargs?: Record<string, unknown>;
}

export interface UnifiedModels {
  task_model?: ModelConfig | null;
  reflection_model: ModelConfig;
  metric_model?: ModelConfig | null;
  extra_reflection_models?: ModelConfig[];
}

export interface UnifiedWorkspace {
  /** `run.py`. */
  run_code: string;
  /** `metric.py`. */
  metric_code: string;
  /** Every other workspace file, relative path to text; only the run code can import them. */
  files?: Record<string, string>;
  metric_timeout_seconds?: number;
  install_command?: string | null;
  dependency_lock?: ScorerDependencyLock | null;
}

export interface UnifiedRunRequest {
  payload_kind: "unified_v1";
  name?: string | null;
  description?: string | null;
  username?: string | null;
  task: UnifiedTask;
  data: UnifiedData;
  optimizer: UnifiedOptimizer;
  models: UnifiedModels;
  workspace: UnifiedWorkspace;
  seed?: number | null;
  token_source?: "managed" | "byok" | "subscription";
  is_private?: boolean;
  economy_mode?: boolean;
  preflight_id?: string | null;
  preflight_fingerprint?: string | null;
  execution_budget_id?: string | null;
  execution_budget_revision?: number | null;
  execution_budget_generation?: number | null;
  max_cost_cents?: number | null;
  estimated_cents_low?: number | null;
  estimated_cents_high?: number | null;
}

/** How a unified run's cases were split; selection and reporting may use different splits. */
export interface UnifiedSplitCounts {
  train: number;
  val: number;
  test: number;
  held_out: boolean;
  selection_split: "val" | "train" | "all";
  report_split: "test" | "val" | "all";
}

export interface OptimizerOption {
  id: OptimizerChoice;
  family: OptimizerFamily;
  available: boolean;
  unavailable_reason?: string | null;
  needs_reflection_model: boolean;
  uses_feedback: boolean;
  supports_iteration_limit: boolean;
  native_proposer: boolean;
}

export interface OptimizerCatalogResponse {
  artifact_kind: OptimizerTarget;
  data_mode: DataMode;
  /** The run code's entry point; the same for every optimizer. */
  run_entry: string;
  /** The metric's exact signature, as the coding agent is told; the same for every optimizer. */
  metric_signature: string;
  default: OptimizerChoice;
  options: OptimizerOption[];
  auto_resolves_to: string[];
  sandbox_available: boolean;
  sandbox_reason?: string | null;
}

export type ContractCheckKey =
  | "workspace.run_signature"
  | "workspace.files"
  | "metric.signature"
  | "metric.model"
  | "workspace.run"
  | "metric.shape";

export interface ContractCheckItem {
  key: ContractCheckKey;
  status: "passed" | "failed" | "pending" | "skipped";
  /** Hebrew base rendering; render `message_code` in the active locale instead. */
  message: string;
  /** Backend i18n code for `message`, resolved with `tI18n`; absent on older servers. */
  message_code?: string | null;
  message_params?: Record<string, string | number>;
  sample_index?: number | null;
  /** Exact error text, trimmed to frames from the user's own code. */
  detail?: string | null;
}

export interface ContractSample {
  index: number;
  input_preview: string;
  output_preview?: string | null;
  score?: number | null;
  feedback_preview?: string | null;
  duration_ms?: number | null;
}

export interface ContractCheckRequest {
  spec: UnifiedRunRequest;
  sample_count?: number;
}

export interface ContractCheckResponse {
  ok: boolean;
  optimizer_resolved: string;
  checks: ContractCheckItem[];
  samples: ContractSample[];
  fingerprint: string;
  /** Whether the metric calls a model, from the server's introspection; absent on older servers. */
  uses_llm?: boolean;
  /** Each Python file's lint, so each file shows its own findings; absent on older servers. */
  files?: WorkspaceFileLint[];
  /** The sandboxed check's redacted terminal log; absent on the static check and older servers. */
  terminal?: CheckTerminalLog | null;
}

/** A Check row the terminal log files its events under. */
export type CheckTerminalPhase = "budget" | "sandbox" | "evaluator";

interface CheckTerminalEventBase {
  /** Milliseconds since the check started. */
  t: number;
  phase: CheckTerminalPhase;
  /** Zero-based evaluator sample the event belongs to. */
  sample?: number;
}

/**
 * One line of the Check's live log. Output is already redacted and clipped by
 * the server; `note` codes are worded by the client.
 */
export type CheckTerminalEvent = CheckTerminalEventBase &
  (
    | { kind: "note"; note: string; params?: Record<string, string | number> }
    | { kind: "command"; id: number; label: string }
    | { kind: "output"; id: number; stream: "stdout" | "stderr" | "log"; text: string }
    | {
        kind: "exit";
        id: number;
        duration_ms: number;
        exit_code?: number;
        timed_out?: boolean;
        error?: string;
      }
    | {
        kind: "sample";
        total: number;
        state: "running" | "done" | "failed";
        score?: number;
        error?: string;
        duration_ms?: number;
      }
  );

export interface CheckTerminalLog {
  version: number;
  events: CheckTerminalEvent[];
  /** Phases whose output hit the server's cap. */
  truncated: CheckTerminalPhase[];
  /** Set when an identical earlier check answered, so this is its stored log. */
  reused?: boolean;
}

/** Lint lines are `"<path>:<line>:<col> <rule> <message>"`. */
export interface WorkspaceFileLint {
  path: string;
  errors: string[];
  warnings: string[];
}

/** One file's verdict from the coding agent's `verify_code` check. */
export interface FileVerification {
  path: string;
  ok: boolean;
  errors: string[];
  warnings: string[];
}

export interface UnifiedChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface InterviewOption {
  label: string;
  description?: string;
}

export interface TaskInterviewRequest {
  turns?: UnifiedChatTurn[];
  objective?: string;
  background?: string;
  artifact_kind?: ArtifactKind | null;
  starting_version?: UnifiedCandidate | null;
  sample_cases?: Array<Record<string, unknown>>;
  target?: OptimizerTarget | null;
  repo_context?: TaskInterviewRepoContext | null;
  locale?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  /** Runs a local CLI model in its fast mode; the backend ignores it elsewhere. */
  fast_mode?: boolean;
}

/** The chosen repository's README and file tree, cut to the server's caps. */
export interface TaskInterviewRepoContext {
  repository: string;
  readme: string;
  paths: string[];
  truncated: boolean;
}

export interface TaskInterviewTurnResult {
  message: string;
  options: InterviewOption[];
  done: boolean;
  objective: string;
  background: string;
  artifact_kind?: ArtifactKind | null;
  starting_version?: UnifiedCandidate | null;
  artifact_rationale: string;
  target?: OptimizerTarget | null;
  /** The repository the user named: `owner/name` for a GitHub link, else their words. */
  repo_query?: string;
  /** Whether the run needs example data; null while the interview cannot tell. */
  needs_data?: boolean | null;
  model?: string | null;
  served_model?: string | null;
}

export interface InputsInferenceRequest {
  objective: string;
  background?: string;
  artifact_kind: OptimizerTarget;
  starting_version?: UnifiedCandidate | null;
  columns?: string[];
  sample_rows?: Array<Record<string, unknown>>;
  row_count?: number;
  user_note?: string;
  locale?: string | null;
  model?: string | null;
}

export interface InputsInferenceResponse {
  mode: DataMode;
  input_fields: string[];
  expected_field?: string | null;
  single_input?: Record<string, unknown> | null;
  rationale: string;
  suggested_split: SplitFractions;
  held_out_count: number;
  warnings: string[];
  model?: string | null;
}

export interface UnifiedAuthoringContext {
  /** A repository run has no artifact, so its task carries none. */
  task: Omit<UnifiedTask, "artifact"> & { artifact?: UnifiedArtifact };
  data_mode: DataMode;
  input_fields?: string[];
  expected_field?: string | null;
  sample_cases?: Array<Record<string, unknown>>;
  optimizer: OptimizerChoice;
  reflection_name?: string | null;
  focus?: "run" | "metric";
  /** "repo" for a repository run: no run code, the agent writes only the repository scorer. */
  target?: "artifact" | "repo";
  /** A repository run's file paths, which the agent may open read-only. */
  repo_files?: string[];
}

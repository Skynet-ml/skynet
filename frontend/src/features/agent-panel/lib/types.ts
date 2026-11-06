import type { WorkflowSpec } from "@/shared/types/api";
import type { DataMode, OptimizerChoice, OptimizerTarget } from "@/shared/types/unified-run";

/** Trust mode — how much the agent must ask before acting. */
export type TrustMode = "ask" | "auto_safe" | "yolo";

/** One prior turn sent with every generalist SSE request. */
export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
  // Compact trace of the tools an assistant turn ran, so the next turn can
  // reason about earlier results instead of only the prose reply.
  tool_calls?: Array<{ tool: string; status: "done" | "error"; result?: string }>;
}

/**
 * Snapshot of the submit wizard exposed to the generalist agent.
 *
 * Mirrors the backend ``WizardState``: the keys ``tools_for`` reads to
 * phase tool exposure, and the write surface for ``update_wizard_state`` —
 * every field the agent may patch flows 1:1 into the wizard's own state.
 */
export interface WizardState {
  job_name?: string;
  job_description?: string;
  is_private?: boolean;
  task_objective?: string;
  task_background?: string;
  /** `"repo"` when the run optimizes a GitHub repository. */
  artifact_kind?: OptimizerTarget;
  starting_version?: string | Record<string, string>;
  /** The repository a repository run optimizes, as `owner/name`. */
  repository?: string;
  /** Whether the user linked a GitHub account; unset until the wizard knows. */
  github_connected?: boolean;
  data_mode?: DataMode;
  dataset_ready?: boolean;
  columns_configured?: boolean;
  staged_dataset_id?: string;
  // Id of a saved library dataset the run submits by reference (durable),
  // set when the user picks from the library instead of uploading. Mutually
  // exclusive with staged_dataset_id.
  source_dataset_id?: string;
  dataset_columns?: string[];
  input_fields?: string[];
  expected_field?: string;
  split_fractions?: { train: number; val: number; test: number };
  shuffle?: boolean;
  seed?: number;
  optimizer_choice?: OptimizerChoice;
  // The code agent still authors through these until it moves to the
  // unified workspace; the backend ignores them.
  optimizer_name?: string;
  workflow?: WorkflowSpec;
  model_config?: Record<string, unknown>;
  model_configured?: boolean;
  reflection_model_config?: Record<string, unknown>;
  /** The run's spending limit in cents; null = no cap. */
  max_cost_cents?: number | null;
  /** The least the spending limit may be: the low end of the run's estimate, in cents. */
  spending_floor_cents?: number;
  run_code?: string;
  metric_code?: string;
  /** The wizard screen on view; the agent may edit only the fields on it. */
  wizard_screen?: string;
  /** Notes screen agents left for later screens, oldest first. */
  screen_notes?: ScreenNote[];
  /** What the Continue button refuses on the screen on view. */
  screen_issue?: string;
  /** The setup is a copy of an earlier run, so the agent waits for the user. */
  cloned_run?: boolean;
}

/** A note one wizard screen's agent left for the screens after it. */
export interface ScreenNote {
  screen: string;
  note: string;
}

export interface ToolStartPayload {
  id: string;
  tool: string;
  reason: string;
  arguments: Record<string, unknown>;
}

export interface ToolEndPayload {
  id: string;
  tool: string;
  status: string;
  result?: unknown;
}

export interface PendingApprovalPayload {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
}

export interface ApprovalResolvedPayload {
  id: string;
  tool: string;
  approved: boolean;
}

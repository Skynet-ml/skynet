/**
 * Wizard v2 optimizer rules and the run types the wizard and stored-run views
 * share. The optimizer rules mirror the backend, so the wizard shows the same
 * choices the server will accept.
 */
/** What the optimizer catalog is keyed on: a repository run. */
export type OptimizerTarget = "repo";
export type DataMode = "single" | "pairs";
export type OptimizerChoice =
  | "auto"
  | "gepa"
  | "best_of_n"
  | "autoresearch"
  | "meta_harness"
  | "autosaddler"
  | "shinka_evolve";
export type OptimizerFamily = "auto" | "blackbox";

export const BLACKBOX_OPTIMIZERS = [
  "gepa",
  "best_of_n",
  "autoresearch",
  "meta_harness",
  "autosaddler",
  "shinka_evolve",
] as const;
// "auto" counts because its resolution consumes feedback text.
const FEEDBACK_OPTIMIZERS: ReadonlySet<string> = new Set(["gepa", "auto"]);
export const MIN_PAIRS_FOR_HELD_OUT = 5;

/** The optimizer choices a repository run accepts: Auto and the six repository engines. */
export function optimizersFor(): OptimizerChoice[] {
  return ["auto", ...BLACKBOX_OPTIMIZERS];
}

/** A stored optimizer id as a current choice; any unknown value gives null. */
export function storedOptimizerChoice(value: unknown): OptimizerChoice | null {
  if (typeof value !== "string") return null;
  return value === "auto" || (BLACKBOX_OPTIMIZERS as readonly string[]).includes(value)
    ? (value as OptimizerChoice)
    : null;
}

/** The optimizer that actually runs: the choice itself, Auto included. */
export function resolveOptimizer(choice: OptimizerChoice): string {
  return choice;
}

export function optimizerFamily(resolved: string): OptimizerFamily {
  return resolved === "auto" ? "auto" : "blackbox";
}

/** Whether the resolved optimizer reads the metric's feedback text. */
export function usesFeedback(choice: OptimizerChoice): boolean {
  return FEEDBACK_OPTIMIZERS.has(resolveOptimizer(choice));
}

export interface UnifiedCase {
  input: Record<string, unknown>;
  expected?: unknown;
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

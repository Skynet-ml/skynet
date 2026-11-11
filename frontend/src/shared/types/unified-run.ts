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

/** Optimizer choices that fit a target and data mode: Auto and the six repository engines. */
export function optimizersFor(target: OptimizerTarget, mode: DataMode): OptimizerChoice[] {
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
export function resolveOptimizer(choice: OptimizerChoice, target: OptimizerTarget): string {
  return choice;
}

export function optimizerFamily(resolved: string): OptimizerFamily {
  return resolved === "auto" ? "auto" : "blackbox";
}

/** Whether the resolved optimizer reads the metric's feedback text. */
export function usesFeedback(choice: OptimizerChoice, target: OptimizerTarget): boolean {
  return FEEDBACK_OPTIMIZERS.has(resolveOptimizer(choice, target));
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

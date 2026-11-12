import type { BlackboxProposer } from "@/shared/types/api";
import { resolveOptimizer, type OptimizerChoice } from "@/shared/types/unified-run";

/**
 * Whether the run's proposer is a coding agent. Auto and the agent-driven
 * engines drive one; GEPA and Best-of-N call their
 * optimization model directly, and ShinkaEvolve edits through its own editor.
 */
export function usesNativeProposer(choice: OptimizerChoice): boolean {
  const resolved = resolveOptimizer(choice);
  return (
    resolved === "auto" ||
    resolved === "meta_harness" ||
    resolved === "autoresearch" ||
    resolved === "autosaddler"
  );
}

/**
 * What a fresh wizard sends when the user never touches the proposer settings.
 *
 * The stall timeout has no control: half an hour without a new score covers a
 * slow scorer pass while still cutting a wedged agent loose.
 */
export const DEFAULT_PROPOSER: BlackboxProposer = {
  harness: "codex",
  max_candidates_per_iter: 3,
  ralph: true,
  max_no_eval_seconds: 1_800,
  max_tool_calls: 100,
};

/** The inclusive range the backend accepts for `max_tool_calls`. */
export const MAX_TOOL_CALLS_LIMITS = { min: 1, max: 1_000 } as const;

/** Which engine-specific proposer knobs the choice exposes; black-box Auto may run any engine. */
export function proposerKnobs(choice: OptimizerChoice): { candidates: boolean; ralph: boolean } {
  const resolved = resolveOptimizer(choice);
  const auto = resolved === "auto";
  return {
    candidates: auto || resolved === "meta_harness",
    ralph: auto || resolved === "autoresearch",
  };
}

/** Whether the resolved engine honours `budget.max_iterations`. */
export function supportsIterationLimit(choice: OptimizerChoice): boolean {
  const resolved = resolveOptimizer(choice);
  return (
    resolved === "meta_harness" || resolved === "autosaddler" || resolved === "shinka_evolve"
  );
}

/** Black-box Auto explores with five engines and then continues the leader: one scorer run each at least. */
export const AUTO_MIN_SCORER_RUNS = 6;

/** The smallest scorer-run budget the resolved optimizer can spend. */
export function minScorerRuns(choice: OptimizerChoice): number {
  return resolveOptimizer(choice) === "auto" ? AUTO_MIN_SCORER_RUNS : 1;
}

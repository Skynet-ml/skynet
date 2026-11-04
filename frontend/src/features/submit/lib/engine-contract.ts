import type {
  BlackboxProposer,
  BlackboxShinkaSettings,
} from "@/shared/types/api";
import {
  resolveOptimizer,
  type OptimizerTarget,
  type OptimizerChoice,
} from "@/shared/types/unified-run";

/**
 * Whether the run's proposer is a coding agent. Auto and the agent-driven
 * engines drive one; DSPy optimizers, GEPA and Best-of-N call their
 * optimization model directly, and ShinkaEvolve edits through its own editor.
 */
export function usesNativeProposer(choice: OptimizerChoice, kind: OptimizerTarget): boolean {
  const resolved = resolveOptimizer(choice, kind);
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
export function proposerKnobs(
  choice: OptimizerChoice,
  kind: OptimizerTarget,
): { candidates: boolean; ralph: boolean } {
  const resolved = resolveOptimizer(choice, kind);
  const auto = resolved === "auto";
  return {
    candidates: auto || resolved === "meta_harness",
    ralph: auto || resolved === "autoresearch",
  };
}

/** The proposer block to submit: knobs the form hides for this choice go back to their defaults. */
export function submittedProposer(
  proposer: BlackboxProposer,
  choice: OptimizerChoice,
  kind: OptimizerTarget,
): BlackboxProposer {
  const knobs = proposerKnobs(choice, kind);
  return {
    ...proposer,
    max_candidates_per_iter: knobs.candidates ? (proposer.max_candidates_per_iter ?? null) : null,
    // Research rounds always run until the budget is spent; stop-at-score ends them early.
    ralph: true,
    max_no_eval_seconds: knobs.ralph ? DEFAULT_PROPOSER.max_no_eval_seconds : null,
    max_tool_calls: proposer.max_tool_calls ?? DEFAULT_PROPOSER.max_tool_calls,
  };
}

/**
 * The run's whole proposer block. A coding-agent proposer sends its settings;
 * ShinkaEvolve has none of its own, so it sends a block only for the agent
 * editor's tool-call cap, with the backend's default harness unused. A null
 * `shinka` means the panel was never touched, which is the single-call editor.
 */
export function submittedRunProposer(input: {
  proposer: BlackboxProposer;
  choice: OptimizerChoice;
  kind: OptimizerTarget;
  shinka: BlackboxShinkaSettings | null;
}): BlackboxProposer | undefined {
  const { proposer, choice, kind, shinka } = input;
  if (usesNativeProposer(choice, kind)) return submittedProposer(proposer, choice, kind);
  if (resolveOptimizer(choice, kind) !== "shinka_evolve" || shinka?.editor !== "agent")
    return undefined;
  return {
    harness: DEFAULT_PROPOSER.harness,
    max_tool_calls: proposer.max_tool_calls ?? DEFAULT_PROPOSER.max_tool_calls,
  };
}

/** Whether the resolved engine honours `budget.max_iterations`. */
export function supportsIterationLimit(choice: OptimizerChoice, kind: OptimizerTarget): boolean {
  const resolved = resolveOptimizer(choice, kind);
  return (
    resolved === "meta_harness" || resolved === "autosaddler" || resolved === "shinka_evolve"
  );
}

/** Black-box Auto explores with five engines and then continues the leader: one scorer run each at least. */
export const AUTO_MIN_SCORER_RUNS = 6;

/** The smallest scorer-run budget the resolved optimizer can spend. */
export function minScorerRuns(choice: OptimizerChoice, kind: OptimizerTarget): number {
  return resolveOptimizer(choice, kind) === "auto" ? AUTO_MIN_SCORER_RUNS : 1;
}

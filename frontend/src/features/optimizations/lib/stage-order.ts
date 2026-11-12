/**
 * Which of the optional stages a run goes through, and how a detected stage
 * maps onto a run's plan. Kept free of runtime imports so it unit-tests
 * without the app's module aliases.
 */
import type {
  BlackboxStrategy,
  OptimizationPayloadResponse,
  OptimizationStatusResponse,
} from "@/shared/types/api";
import type { PipelineStage } from "../constants";

const STAGE_ORDER: readonly PipelineStage[] = [
  "validating",
  "splitting",
  "baseline",
  "optimizing",
  "refining",
  "evaluating",
];

/**
 * The engine of a single-engine run, or null for the auto strategy. The run's
 * own lane event wins; before it lands, the
 * submitted strategy and then the result name the engine.
 */
export function singleEngineOf(
  job: OptimizationStatusResponse,
  payload: OptimizationPayloadResponse | null | undefined,
): string | null {
  const singleLane = job.progress_events?.find(
    (e) => e.event === "lane_started" && e.metrics?.phase === "single",
  );
  const laneEngine = singleLane?.metrics?.engine;
  if (typeof laneEngine === "string") return laneEngine;
  const strategy = payload?.payload.strategy as Partial<BlackboxStrategy> | undefined;
  if (strategy?.mode === "single")
    return strategy.engine ?? job.blackbox_result?.engine_used ?? null;
  // Without the payload (a share view, the detail gate's probe) the finished
  // result still records the strategy it ran.
  if (!strategy && job.blackbox_result?.strategy_mode === "single")
    return job.blackbox_result.engine_used;
  return null;
}

/**
 * The keys of every stage a run's pipeline tracker shows, in order. The
 * tracker (`planPipelineStages`) labels these, and the loading skeleton
 * remembers how many there are, so both always agree on the count.
 */
export function plannedStageKeys(
  job: OptimizationStatusResponse,
  payload: OptimizationPayloadResponse | null | undefined,
): PipelineStage[] {
  // The auto strategy races explore lanes, then refines the winner with GEPA.
  const middle: PipelineStage[] =
    singleEngineOf(job, payload) === null ? ["optimizing", "refining"] : ["optimizing"];
  // Runs never split cases up front and score the starting point in the final
  // evaluation, so there is no splitting or baseline stage.
  return ["validating", ...middle, "evaluating"];
}

/**
 * Map a detected stage onto the plan. Stage detection is shared across run
 * kinds, so it can name a stage this run skips (a black-box run reads as
 * "splitting" before its first candidate); the run is then in the next
 * stage the plan does list.
 */
export function stageInPlan(
  plan: ReadonlyArray<{ key: PipelineStage }>,
  stage: PipelineStage | "done",
): PipelineStage | "done" {
  if (stage === "done") return stage;
  const keys = new Set(plan.map((s) => s.key));
  return STAGE_ORDER.slice(STAGE_ORDER.indexOf(stage)).find((k) => keys.has(k)) ?? "done";
}

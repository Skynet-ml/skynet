/**
 * Per-run pipeline plan for the stage tracker.
 *
 * Every flow validates and evaluates the winner. Runs never split cases up
 * front, since they score the starting point in the final evaluation. The
 * middle differs by algorithm: a single engine, or the auto strategy's
 * parallel exploration followed by a GEPA refinement lane. The plan lists only the stages this run goes through,
 * so the tracker reads as the run that actually happened.
 */
import type { OptimizationPayloadResponse, OptimizationStatusResponse } from "@/shared/types/api";
import { TERMS } from "@/shared/lib/terms";
import { formatMsg, msg } from "@/shared/lib/messages";
import { engineDisplayName } from "@/features/explore";
import type { PipelineStage } from "../constants";
import { plannedStageKeys, singleEngineOf } from "./stage-order";

export { stageInPlan } from "./stage-order";

export interface PlannedStage {
  key: PipelineStage;
  label: string;
  /** The algorithm behind the stage, shown under the label. */
  detail?: string;
}

const HILL_CLIMBING_ENGINES = new Set([
  "autoresearch",
  "meta_harness",
  "autosaddler",
  "shinka_evolve",
]);

function middleStages(
  job: OptimizationStatusResponse,
  payload: OptimizationPayloadResponse | null | undefined,
): PlannedStage[] {
  const singleEngine = singleEngineOf(job, payload);
  if (singleEngine) {
    const id = singleEngine.toLowerCase();
    const label =
      id === "gepa"
        ? msg("pipeline.stage.reflectiveSearch")
        : id === "best_of_n"
          ? msg("pipeline.stage.sampling")
          : HILL_CLIMBING_ENGINES.has(id)
            ? msg("pipeline.stage.hillClimbing")
            : TERMS.optimization;
    return [{ key: "optimizing", label, detail: engineDisplayName(singleEngine) }];
  }

  // The auto strategy (the default) races explore lanes, then hands the best
  // candidate to a GEPA lane to refine.
  const exploreEngines = new Set(
    (job.progress_events ?? [])
      .filter((e) => e.event === "lane_started")
      .filter((e) => e.metrics?.phase === "explore")
      .map((e) => e.metrics?.engine)
      .filter((engine): engine is string => typeof engine === "string"),
  );
  return [
    {
      key: "optimizing",
      label: msg("pipeline.stage.exploration"),
      detail:
        exploreEngines.size > 0
          ? formatMsg("pipeline.stage.lanes", { p1: exploreEngines.size })
          : undefined,
    },
    { key: "refining", label: msg("pipeline.stage.refinement"), detail: "GEPA" },
  ];
}

export function planPipelineStages(
  job: OptimizationStatusResponse,
  payload: OptimizationPayloadResponse | null | undefined,
): PlannedStage[] {
  // The stage keys (and so the count the loading skeleton remembers) come from
  // `plannedStageKeys`; this only labels them.
  const middle = middleStages(job, payload);
  const fixed: Partial<Record<PipelineStage, PlannedStage>> = {
    validating: {
      key: "validating",
      label: msg("auto.features.optimizations.constants.literal.1"),
    },
    evaluating: {
      key: "evaluating",
      label: msg("auto.features.optimizations.constants.literal.3"),
    },
  };
  return plannedStageKeys(job, payload).map(
    (key) => fixed[key] ?? middle.find((s) => s.key === key) ?? { key, label: TERMS.optimization },
  );
}

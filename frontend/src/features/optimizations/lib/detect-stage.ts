/**
 * Pipeline-stage detection for an in-progress optimization.
 *
 * Looks at a job's progress event stream + latest metrics to classify
 * what step of the pipeline it is currently executing.
 */
import type { OptimizationStatusResponse } from "@/shared/types/api";
import type { PipelineStage } from "../constants";

export function detectStage(job: OptimizationStatusResponse): PipelineStage {
  if (job.status === "validating") return "validating";
  if (job.status === "success") return "done";

  const events = job.progress_events ?? [];
  const eventNames = events.map((e) => e.event);
  const metrics = job.latest_metrics ?? {};

  if (eventNames.includes("optimized_evaluated")) return "done";
  // Every flow fires this once its optimizer is done
  // and the final held-out scoring of the winner begins.
  if (eventNames.includes("evaluation_started")) return "evaluating";
  // The black-box auto strategy hands its best explore candidate to a GEPA
  // lane; from then until the final evaluation the run is refining.
  if (eventNames.includes("lane_handoff")) return "refining";
  if (eventNames.includes("optimizer_progress")) return "optimizing";
  if (eventNames.includes("baseline_evaluated")) return "optimizing";
  // Engines stream candidate-level events instead of "optimizer_progress"
  // bars. Once any candidate is recorded, the run is past baseline
  // measurement and iterating.
  if (eventNames.includes("candidate") || eventNames.includes("candidate_rejected"))
    return "optimizing";

  // Fallback: use latest_metrics hints (e.g. tqdm from optimizer)
  if (metrics.tqdm_desc || metrics.tqdm_percent != null) return "optimizing";

  if (job.status === "running") return "splitting";
  return "validating";
}
